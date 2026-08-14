"""Route tests for commute intelligence, alerts and area comparison.

The rule these enforce: where we have no data, the API says so. It never
substitutes a plausible-looking number, because the product's entire claim is
that the numbers are real tenant reports.
"""

from __future__ import annotations

import pytest

from app.core import geohash
from app.db.models import (
    LGA,
    CommuteDestination,
    CommuteReport,
    Neighbourhood,
    Property,
    TransitOption,
)
from tests.conftest import TENANT_PHONE, auth, login

PROPERTY_ID = "ETI-LEK-7F3A2B-0041"


@pytest.fixture
async def commute_fixture(session_factory):
    async with session_factory() as s:
        s.add(LGA(code="ETI", name="Eti-Osa", flood_risk="VeryHigh"))
        s.add(
            Neighbourhood(
                code="LEK",
                name="Lekki Phase 1",
                lga_code="ETI",
                avg_rent_2bed=280_000_000,
                avg_rating=3.9,
                avg_power_hours=15,
                avg_agent_fee_pct=11.2,
                commute_vi_min=35,
                flood_risk="High",
                bottleneck_title="Single-corridor risk",
                bottleneck_detail="One expressway serves the whole area.",
            )
        )
        s.add(
            Neighbourhood(
                code="YAB",
                name="Yaba",
                lga_code="ETI",
                avg_rent_2bed=120_000_000,
                avg_rating=3.6,
                avg_power_hours=11,
                avg_agent_fee_pct=9.8,
                commute_vi_min=75,
                flood_risk="Moderate",
            )
        )
        s.add(CommuteDestination(code="VI", name="Victoria Island"))
        s.add(CommuteDestination(code="IKJ", name="Ikeja GRA"))

        gh8, gh7 = geohash.encode_pair(6.4474, 3.4736)
        prop = Property(
            property_id=PROPERTY_ID,
            geohash_7=gh7,
            geohash_8=gh8,
            lat=6.4474,
            lng=3.4736,
            address_local="12A Admiralty Way",
            lga_code="ETI",
            neighbourhood_code="LEK",
        )
        s.add(prop)
        await s.flush()

        for window, minutes in [
            ("am_rush", 95),
            ("am_rush", 105),
            ("am_rush", 130),
            ("midday", 30),
        ]:
            s.add(
                CommuteReport(
                    property_id=prop.id,
                    destination_code="VI",
                    departure_window=window,
                    mode="car",
                    minutes=minutes,
                )
            )
        s.add(
            TransitOption(
                property_id=prop.id, kind="brt", label="BRT stop", distance_m=400
            )
        )
        await s.commit()
        return prop


async def test_commute_aggregates_tenant_reports(client, commute_fixture):
    r = await client.get(f"/properties/{PROPERTY_ID}/commute?destination=VI")
    assert r.status_code == 200
    data = r.json()

    assert data["report_count"] == 4
    assert data["typical_min"] == 100          # median of 30, 95, 105, 130
    assert data["fastest_min"] == 30
    assert data["slowest_min"] == 130

    am = next(w for w in data["by_window"] if w["window"] == "am_rush")
    assert am["typical_min"] == 105
    assert am["worst_min"] == 130
    assert am["report_count"] == 3


async def test_commute_never_invents_a_routing_estimate(client, commute_fixture):
    """No Routes integration exists, so this stays null rather than guessing."""
    data = (
        await client.get(f"/properties/{PROPERTY_ID}/commute?destination=VI")
    ).json()
    assert data["google_estimate_min"] is None


async def test_commute_with_no_reports_returns_nulls_not_numbers(
    client, commute_fixture
):
    data = (
        await client.get(f"/properties/{PROPERTY_ID}/commute?destination=IKJ")
    ).json()
    assert data["report_count"] == 0
    assert data["typical_min"] is None
    assert data["by_window"] == []
    # Transit and corridor risk are property/area facts, so they still apply.
    assert len(data["transit"]) == 1
    assert data["bottleneck"]["title"] == "Single-corridor risk"


async def test_commute_rejects_unknown_destination(client, commute_fixture):
    r = await client.get(f"/properties/{PROPERTY_ID}/commute?destination=NOPE")
    assert r.status_code == 404


async def test_tenant_can_add_their_own_commute_time(client, users, commute_fixture):
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        f"/properties/{PROPERTY_ID}/commute",
        json={
            "destination_code": "IKJ",
            "departure_window": "am_rush",
            "mode": "bus",
            "minutes": 150,
            "note": "Two danfos.",
        },
        headers=auth(token),
    )
    assert r.status_code == 201
    assert r.json()["report_count"] == 1
    assert r.json()["typical_min"] == 150
    assert r.json()["notes"] == ["Two danfos."]


async def test_adding_a_commute_time_requires_sign_in(client, commute_fixture):
    r = await client.post(
        f"/properties/{PROPERTY_ID}/commute",
        json={
            "destination_code": "VI",
            "departure_window": "am_rush",
            "mode": "car",
            "minutes": 60,
        },
    )
    assert r.status_code == 401


async def test_invalid_departure_window_is_rejected(client, users, commute_fixture):
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        f"/properties/{PROPERTY_ID}/commute",
        json={
            "destination_code": "VI",
            "departure_window": "whenever",
            "mode": "car",
            "minutes": 60,
        },
        headers=auth(token),
    )
    assert r.status_code == 422


async def test_area_counts_are_computed_not_read_from_stale_columns(
    client, commute_fixture
):
    """`total_properties`/`total_reviews` were seeded to 0 and never maintained."""
    areas = (await client.get("/neighbourhoods")).json()
    lek = next(a for a in areas if a["code"] == "LEK")
    assert lek["total_properties"] == 1
    assert lek["total_reviews"] == 0  # none approved yet


async def test_compare_returns_both_areas(client, commute_fixture):
    r = await client.get("/neighbourhoods/compare?codes=LEK,YAB")
    assert r.status_code == 200
    areas = r.json()["areas"]
    assert [a["code"] for a in areas] == ["LEK", "YAB"]
    assert areas[0]["avg_rent_2bed"] == 280_000_000


async def test_alerts_only_surface_approved_reviews(client, users, commute_fixture):
    """The activity feed must not become a side channel for held content."""
    token = await login(client, TENANT_PHONE)
    await client.post(
        "/reviews",
        json={
            "property_id": PROPERTY_ID,
            "tenancy_start": "2023-01-01",
            "still_living": False,
            "rent_amount_kobo": 150_000_000,
            "ratings": dict.fromkeys(
                [
                    "landlord", "agent", "property", "water", "power",
                    "security", "noise", "flooding", "neighbourhood", "value",
                ],
                4,
            ),
            "text_positives": "Water is constant and the estate is quiet.",
            "text_warnings": "The agent is a fraudster and a thief",
            "is_anonymous": False,
        },
        headers=auth(token),
    )

    alerts = (await client.get("/alerts")).json()
    assert not any(a["kind"] == "review" for a in alerts)

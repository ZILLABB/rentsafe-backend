"""Address lookup and self-registration.

Registering your own building is the thing that makes RentSafe usable by
someone who doesn't already appear in the database. The identity endpoint
existed from the start; nothing could reach it because there was no way to turn
an address into the LGA and area code it needs.
"""

from __future__ import annotations

import pytest

from app.api.v1.places import MAX_AREA_DISTANCE_M
from app.db.models import LGA, Neighbourhood
from app.services import opendata
from tests.conftest import TENANT_PHONE, auth, login


@pytest.fixture
async def lagos_areas(session_factory):
    async with session_factory() as s:
        s.add(LGA(code="ETI", name="Eti-Osa", centroid_lat=6.4550, centroid_lng=3.4350))
        s.add(LGA(code="IKE", name="Ikeja", centroid_lat=6.6251, centroid_lng=3.3177))
        s.add(
            Neighbourhood(
                code="LEK", name="Lekki Phase 1", lga_code="ETI",
                centroid_lat=6.4478, centroid_lng=3.4723,
            )
        )
        s.add(
            Neighbourhood(
                code="ORE", name="Oregun", lga_code="IKE",
                centroid_lat=6.6100, centroid_lng=3.3600,
            )
        )
        await s.commit()


@pytest.fixture(autouse=True)
def cached_geocode(tmp_path, monkeypatch):
    """Never hit Nominatim from a test — its usage policy caps automated use."""
    monkeypatch.setattr(opendata, "CACHE_DIR", tmp_path)


async def test_resolve_maps_a_point_to_its_area(client, lagos_areas):
    r = await client.get("/places/resolve?lat=6.4474&lng=3.4736")
    assert r.status_code == 200
    body = r.json()
    assert body["area_code"] == "LEK"
    assert body["lga_code"] == "ETI"
    assert body["distance_m"] < 500


async def test_resolve_picks_the_nearest_area(client, lagos_areas):
    body = (await client.get("/places/resolve?lat=6.6090&lng=3.3610")).json()
    assert body["area_code"] == "ORE"
    assert body["lga_name"] == "Ikeja"


async def test_resolve_refuses_to_guess_far_from_any_known_area(client, lagos_areas):
    """Better to say we don't cover it than to file a home under a distant suburb."""
    # Abuja — nowhere near a Lagos centroid.
    body = (await client.get("/places/resolve?lat=9.0765&lng=7.3986")).json()
    assert body["area_code"] is None
    assert body["distance_m"] > MAX_AREA_DISTANCE_M


async def test_search_resolves_each_hit_to_an_area(client, lagos_areas, monkeypatch):
    # The handler uses the async geocoder — the sync one would block the loop.
    async def fake_geocode(q, **kw):
        return [
            {
                "label": "Admiralty Way, Lekki Phase I, Eti Osa, Lagos",
                "lat": 6.4478, "lng": 3.4741,
                "road": "Admiralty Way", "suburb": "Lekki Phase I",
                "city": "Lagos", "type": "residential",
            }
        ]

    monkeypatch.setattr(opendata, "geocode_async", fake_geocode)
    hits = (await client.get("/places/search?q=Admiralty Way Lekki")).json()
    assert len(hits) == 1
    assert hits[0]["road"] == "Admiralty Way"
    assert hits[0]["resolved"]["area_code"] == "LEK"
    assert hits[0]["resolved"]["lga_code"] == "ETI"


async def test_search_reports_upstream_failure_rather_than_empty(
    client, lagos_areas, monkeypatch
):
    """An empty list would read as 'no such address', which is a different thing."""

    async def boom(q, **kw):
        raise opendata.OpenDataError("nominatim down")

    monkeypatch.setattr(opendata, "geocode_async", boom)
    r = await client.get("/places/search?q=Admiralty Way")
    assert r.status_code == 503


async def test_search_requires_a_real_term(client, lagos_areas):
    assert (await client.get("/places/search?q=ab")).status_code == 422


async def test_a_tenant_can_register_their_own_address(client, users, lagos_areas):
    """The end-to-end gap this closes: address -> PropertyID -> reviewable."""
    resolved = (await client.get("/places/resolve?lat=6.6100&lng=3.3605")).json()

    r = await client.post(
        "/properties/identify",
        json={
            "lat": 6.6100,
            "lng": 3.3605,
            "lga_code": resolved["lga_code"],
            "area_code": resolved["area_code"],
            "address": "14 Opebi Road, Oregun",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match"] == "created"
    property_id = body["property_id"]
    assert property_id.startswith("IKE-ORE-")

    # It's immediately a real property, with the address shown rather than the ID.
    prop = (await client.get(f"/properties/{property_id}")).json()
    assert prop["address_local"] == "14 Opebi Road, Oregun"
    assert prop["neighbourhood_name"] == "Oregun"
    assert prop["total_reviews"] == 0

    # ...and reviewable.
    token = await login(client, TENANT_PHONE)
    submitted = await client.post(
        "/reviews",
        json={
            "property_id": property_id,
            "tenancy_start": "2023-05-01",
            "still_living": True,
            "rent_amount_kobo": 240_000_000,
            "ratings": dict.fromkeys(
                [
                    "landlord", "agent", "property", "water", "power",
                    "security", "noise", "flooding", "neighbourhood", "value",
                ],
                4,
            ),
            "text_positives": "Borehole water is clean and the gate is manned.",
            "text_warnings": "Opebi traffic at 7am adds an hour.",
            "is_anonymous": False,
        },
        headers=auth(token),
    )
    assert submitted.status_code == 201


async def test_registering_the_same_building_twice_attaches_rather_than_forks(
    client, users, lagos_areas
):
    """Two tenants of one building must land on one PropertyID, or the reviews
    split across duplicate records and neither shows the real picture."""
    payload = {
        "lat": 6.6100, "lng": 3.3605,
        "lga_code": "IKE", "area_code": "ORE",
        "address": "14 Opebi Road",
    }
    first = (await client.post("/properties/identify", json=payload)).json()
    second = (await client.post("/properties/identify", json=payload)).json()

    assert first["match"] == "created"
    assert second["match"] == "existing"
    assert second["property_id"] == first["property_id"]


async def test_anonymous_property_registration_is_rate_limited(
    client, users, lagos_areas
):
    """Open by design, so it needs a ceiling.

    Without one, anyone could pollute the PropertyID namespace indefinitely —
    verified by doing exactly that against the running server.
    """

    payload = {
        "lat": 6.6100, "lng": 3.3605,
        "lga_code": "IKE", "area_code": "ORE",
        "address": "Somewhere",
    }
    codes = []
    for i in range(12):
        # Nudge each one outside the 15m dedup radius so they're real creates.
        body = {**payload, "lat": 6.6100 + i * 0.01}
        codes.append((await client.post("/properties/identify", json=body)).status_code)

    assert 429 in codes, "registration is unlimited for anonymous callers"
    assert codes[0] == 200, "the first registration should still succeed"
    # A household registering their home must not be caught by this.
    assert codes[:5] == [200] * 5, codes


async def test_address_search_is_rate_limited(client, lagos_areas, monkeypatch):
    """Each miss hits Nominatim and writes a cache file — both need bounding."""
    from app.services import opendata as od

    async def fake(q, **kw):
        return []

    monkeypatch.setattr(od, "geocode_async", fake)

    codes = [
        (await client.get(f"/places/search?q=street number {i}")).status_code
        for i in range(70)
    ]
    assert 429 in codes
    assert codes[0] == 200


async def test_rate_limit_is_per_client(client, lagos_areas, monkeypatch):
    """One noisy network must not lock everybody else out.

    Separation comes from the *socket* address unless a proxy is configured, so
    this test declares one hop and sends the header the way a load balancer
    would. Sending it without that configuration is covered below.
    """
    from app.api import ratelimit
    from app.services import opendata as od

    async def fake(q, **kw):
        return []

    monkeypatch.setattr(od, "geocode_async", fake)
    monkeypatch.setattr(ratelimit.settings, "trusted_proxy_hops", 1)

    for i in range(70):
        await client.get(
            f"/places/search?q=noisy {i}", headers={"X-Forwarded-For": "1.2.3.4"}
        )

    other = await client.get(
        "/places/search?q=quiet caller", headers={"X-Forwarded-For": "5.6.7.8"}
    )
    assert other.status_code == 200


async def test_forged_forwarded_for_cannot_mint_a_fresh_quota(
    client, lagos_areas, monkeypatch
):
    """The header is client-controlled. Without a proxy it must be ignored.

    Previously the limiter read X-Forwarded-For unconditionally and took the
    leftmost entry — the one the *client* writes — so anyone could reset their
    own quota by changing a header, defeating this limit and the OTP quota that
    guards the SMS bill.
    """
    from app.api import ratelimit
    from app.services import opendata as od

    async def fake(q, **kw):
        return []

    monkeypatch.setattr(od, "geocode_async", fake)
    monkeypatch.setattr(ratelimit.settings, "trusted_proxy_hops", 0)

    codes = [
        (
            await client.get(
                f"/places/search?q=spoof {i}",
                # A different forged origin every single time.
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            )
        ).status_code
        for i in range(70)
    ]
    assert 429 in codes, "a forged header still bought an unlimited quota"


def test_only_trusted_hops_of_the_chain_are_believed():
    """With one proxy, believe the entry the proxy appended — not the client's."""
    from types import SimpleNamespace

    from app.api import ratelimit

    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.1"),
        headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7"},
    )

    ratelimit.settings.trusted_proxy_hops = 0
    assert ratelimit.client_ip(request) == "10.0.0.1"

    ratelimit.settings.trusted_proxy_hops = 1
    # "9.9.9.9" is whatever the caller claimed; the proxy wrote the rightmost.
    assert ratelimit.client_ip(request) == "203.0.113.7"

    ratelimit.settings.trusted_proxy_hops = 0

"""Listing properties.

The bug this file exists for appeared only once the data grew. With nine
properties the page limit never bit; at 188 the Explore counter said
"50 properties" while 188 matched, because the response gave the client no way
to tell a complete result from a truncated one.

Same failure the agent directory had. It is worth a test rather than a fix,
because it does not reproduce until somebody imports enough rows — which is to
say, in production.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.core import geohash
from app.db.models import LGA, Neighbourhood, Property


@pytest_asyncio.fixture
async def many_properties(session_factory):
    """More properties than a small page, so truncation actually happens."""
    async with session_factory() as s:
        s.add(LGA(code="ETI", name="Eti-Osa"))
        s.add(Neighbourhood(code="LEK", name="Lekki Phase 1", lga_code="ETI"))
        for i in range(12):
            lat, lng = 6.44 + i / 1000, 3.47 + i / 1000
            gh8, gh7 = geohash.encode_pair(lat, lng)
            s.add(
                Property(
                    property_id=f"ETI-LEK-{i:06X}-0000",
                    geohash_7=gh7,
                    geohash_8=gh8,
                    lat=lat,
                    lng=lng,
                    address_local=f"Block {i}",
                    lga_code="ETI",
                    neighbourhood_code="LEK",
                    # A spread of ratings so a filter can bite.
                    avg_rating=4.8 if i < 3 else 2.0,
                    total_reviews=1 if i < 3 else 0,
                )
            )
        await s.commit()


async def test_the_total_is_reported_so_a_page_is_not_mistaken_for_the_set(
    client, many_properties
):
    """A client that cannot see the total reports the page size as the result."""
    r = await client.get("/properties?limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2

    total = r.headers.get("X-Total-Count")
    assert total is not None, "no X-Total-Count header"
    assert int(total) == 12


async def test_the_total_counts_what_matched_not_the_whole_table(
    client, many_properties
):
    """Otherwise a filtered search claims results it did not find."""
    everything = await client.get("/properties?limit=1")
    filtered = await client.get("/properties?limit=1&min_rating=4.5")

    assert int(everything.headers["X-Total-Count"]) == 12
    assert int(filtered.headers["X-Total-Count"]) == 3


async def test_the_total_is_present_even_when_nothing_matches(
    client, many_properties
):
    """Zero is a fact. A missing header would read as "we didn't count"."""
    r = await client.get("/properties?area=NOWHERE")
    assert r.json() == []
    assert r.headers["X-Total-Count"] == "0"


async def test_an_unpaged_result_still_reports_its_size(client, many_properties):
    r = await client.get("/properties")
    assert int(r.headers["X-Total-Count"]) == len(r.json()) == 12


def test_the_header_is_readable_cross_origin():
    """Without expose_headers a browser cannot read it at all, and the client
    silently falls back to treating the page as the whole result."""
    from app.main import app

    cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert "X-Total-Count" in cors.kwargs["expose_headers"]


@pytest.mark.parametrize("limit", [1, 5, 12, 50])
async def test_the_page_never_exceeds_the_limit(client, many_properties, limit):
    r = await client.get(f"/properties?limit={limit}")
    assert len(r.json()) <= limit
    # …and the total is unaffected by how much of it was returned.
    assert int(r.headers["X-Total-Count"]) == 12

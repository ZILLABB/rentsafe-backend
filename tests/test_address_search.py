"""Lagos addresses have to resolve, and inexact ones must stay honest.

The failure this covers was reported from real use: a tenant typed
"16 salako street magodo" — an address they live at — and was told no Lagos
address matched. OpenStreetMap has no house numbers for most of Lagos and is
missing many residential streets outright, so a single all-or-nothing geocoder
query fails on ordinary addresses.

Widening the query fixes the dead end but introduces two ways to be wrong, and
both are worse than the original failure:

  * matching a same-named street on the other side of the city, and
  * folding every unmapped address in a neighbourhood into one property,
    because they all share the area centroid.

Both are asserted here.
"""

from __future__ import annotations

import pytest

from app.services import identity, opendata

# --- Query widening -------------------------------------------------------


def test_variants_widen_from_full_address_to_area():
    variants = opendata.address_variants("16 Salako Street Magodo")
    assert variants[0] == ("16 Salako Street Magodo", "exact")
    # The house number goes first: OSM almost never has one for Lagos.
    assert ("Salako Street Magodo", "exact") in variants
    assert ("Salako Street", "street") in variants
    assert variants[-1] == ("Magodo", "area")


def test_variants_strip_assorted_house_number_spellings():
    for prefix in ("16", "No 16", "No. 16", "#16", "16a"):
        variants = opendata.address_variants(f"{prefix} Salako Street Magodo")
        assert any(t == "Salako Street Magodo" for t, _ in variants), prefix


def test_variants_ignore_blank_input():
    assert opendata.address_variants("   ") == []


class _FakeGeocoder:
    """Stands in for Nominatim with a fixed, tiny world."""

    def __init__(self, world: dict[str, list[dict]]):
        self.world = world
        self.calls: list[str] = []

    async def __call__(self, term, *, limit=6, offline=False):
        self.calls.append(term)
        return self.world.get(term.lower(), [])


def _hit(label, lat, lng, road=None):
    return {"label": label, "lat": lat, "lng": lng, "road": road, "suburb": None}


@pytest.mark.asyncio
async def test_unmapped_street_falls_back_to_the_area(monkeypatch):
    """The reported case. Only "Magodo" is in the fake world, as in reality."""
    fake = _FakeGeocoder({"magodo": [_hit("Magodo, Kosofe, Lagos", 6.6181, 3.3778)]})
    monkeypatch.setattr(opendata, "geocode_async", fake)

    hits, precision = await opendata.geocode_progressive("16 salako street magodo")

    assert precision == "area"
    assert hits[0]["label"].startswith("Magodo")


@pytest.mark.asyncio
async def test_same_named_street_elsewhere_is_rejected(monkeypatch):
    """Salako Street exists in Ogba, 5.3km from Magodo. It is not this street.

    Returning it would pin a tenant in a different neighbourhood — a confident
    wrong answer, which is worse than the "no match" being fixed here.
    """
    fake = _FakeGeocoder(
        {
            "magodo": [_hit("Magodo, Kosofe, Lagos", 6.6181, 3.3778)],
            "salako street": [
                _hit("Salako Street, Ogba, Lagos", 6.6339, 3.3331, road="Salako Street")
            ],
        }
    )
    monkeypatch.setattr(opendata, "geocode_async", fake)

    hits, precision = await opendata.geocode_progressive("16 salako street magodo")

    assert precision == "area"
    assert "Ogba" not in hits[0]["label"]


@pytest.mark.asyncio
async def test_street_near_its_area_is_kept(monkeypatch):
    """The mirror case: a street match close to the named area is the right one."""
    fake = _FakeGeocoder(
        {
            "magodo": [_hit("Magodo, Kosofe, Lagos", 6.6181, 3.3778)],
            "salako street": [
                _hit("Salako Street, Magodo", 6.6190, 3.3790, road="Salako Street")
            ],
        }
    )
    monkeypatch.setattr(opendata, "geocode_async", fake)

    hits, precision = await opendata.geocode_progressive("16 salako street magodo")

    assert precision == "street"
    assert hits[0]["road"] == "Salako Street"


@pytest.mark.asyncio
async def test_nothing_anywhere_still_reports_no_match(monkeypatch):
    """Widening must not turn "we don't know" into a fabricated pin."""
    monkeypatch.setattr(opendata, "geocode_async", _FakeGeocoder({}))
    hits, precision = await opendata.geocode_progressive("qwertyuiop zzzz")
    assert hits == []
    assert precision == "none"


# --- Dedup under an approximate coordinate --------------------------------

CENTROID = {"lat": 6.6181, "lng": 3.3778, "lga_code": "KOS", "area_code": "MAGODO"}


@pytest.mark.asyncio
async def test_approximate_pins_do_not_merge_different_streets(session_factory):
    """Two Magodo tenants, same centroid, different streets -> two properties.

    Without this, the second registration lands 0m from the first and is
    silently attached to it, hanging one landlord's reviews on another's
    building.
    """
    async with session_factory() as session:
        first = await identity.identify_or_create(
            session, **CENTROID, address="16 Salako Street", location_approximate=True
        )
        assert first.match == "created"

        second = await identity.identify_or_create(
            session, **CENTROID, address="4 Emmanuel Close", location_approximate=True
        )
    # Not auto-attached. The user is asked to pick or add a new building — the
    # honest outcome when the coordinates cannot tell the buildings apart.
    assert second.match != "existing"


@pytest.mark.asyncio
async def test_exact_pins_still_dedup_on_distance(session_factory):
    """The approximate path must not weaken normal registration.

    With a real pin, two submissions at the same point *are* the same building
    and should still merge without asking.
    """
    async with session_factory() as session:
        first = await identity.identify_or_create(
            session, **CENTROID, address="16 Salako Street"
        )
        second = await identity.identify_or_create(
            session, **CENTROID, address="Something else entirely"
        )
    assert first.match == "created"
    assert second.match == "existing"
    assert second.property_id == first.property_id


@pytest.mark.asyncio
async def test_approximate_pin_reattaches_on_the_same_address(session_factory):
    """The same street typed twice is the same building, centroid or not."""
    async with session_factory() as session:
        first = await identity.identify_or_create(
            session, **CENTROID, address="16 Salako Street", location_approximate=True
        )
        again = await identity.identify_or_create(
            # Different spelling, same address: normalisation is what matches.
            session, **CENTROID, address="No. 16 Salako St", location_approximate=True
        )
    assert again.match == "existing"
    assert again.property_id == first.property_id


@pytest.mark.asyncio
async def test_approximate_registration_is_recorded_as_approximate(session_factory):
    """A screen must be able to tell a guessed pin from a real one."""
    from sqlalchemy import select

    from app.db.models import Property

    async with session_factory() as session:
        out = await identity.identify_or_create(
            session, **CENTROID, address="7 Unmapped Way", location_approximate=True
        )
        prop = (
            await session.execute(
                select(Property).where(Property.property_id == out.property_id)
            )
        ).scalar_one()

    assert prop.location_precision == "area"
    # And the tenant's own words are kept, not the geocoder's "Magodo".
    assert prop.address_local == "7 Unmapped Way"


# --- Multi-word area names ------------------------------------------------
#
# Reported after the first fix: "16 salako street magodo phase 1" still found
# nothing. Taking the last token as the area gave "1"; the real area name spans
# three words. Lagos is full of these — Lekki Phase 1, Ikeja GRA, Magodo Phase 2.


def test_area_is_the_longest_suffix_not_the_last_token():
    variants = opendata.address_variants("16 salako street magodo phase 1")
    areas = [t for t, p in variants if p == "area"]
    assert areas[0] == "magodo phase 1"
    # "1" alone is not a place and must never be searched for.
    assert "1" not in areas


def test_street_type_words_never_start_an_area():
    """Splitting after "Salako" would ask for an area called "Street Magodo"."""
    for t, p in opendata.address_variants("16 salako street magodo"):
        if p == "area":
            assert not t.lower().startswith("street")


def test_splits_prefer_the_more_specific_area():
    splits = opendata.split_street_and_area("12 Admiralty Way Lekki Phase 1")
    assert splits[0][1] == "Lekki Phase 1"


@pytest.mark.asyncio
async def test_multiword_area_resolves(monkeypatch):
    """The reported case. Nominatim knows "magodo phase 1"; we never asked it."""
    fake = _FakeGeocoder(
        {"magodo phase 1": [_hit("Magodo, Shangisha, Kosofe, Lagos", 6.6181, 3.3778)]}
    )
    monkeypatch.setattr(opendata, "geocode_async", fake)

    hits, precision = await opendata.geocode_progressive(
        "16 salako street magodo phase 1"
    )

    assert precision == "area"
    assert hits[0]["label"].startswith("Magodo")


@pytest.mark.asyncio
async def test_a_supplied_anchor_is_trusted_over_a_guessed_suffix(monkeypatch):
    """Callers holding real area data should not be second-guessed.

    "phase 1" geocodes to an estate in Eti-Osa. If the caller says the area is
    Magodo, a street match near Eti-Osa is still the wrong street.
    """
    fake = _FakeGeocoder(
        {
            "salako street": [
                _hit("Salako Street, Ogba", 6.6339, 3.3331, road="Salako Street")
            ],
        }
    )
    monkeypatch.setattr(opendata, "geocode_async", fake)

    hits, precision = await opendata.geocode_progressive(
        "16 salako street magodo phase 1", anchor=(6.6181, 3.3778)
    )
    assert precision == "none"
    assert hits == []


@pytest.mark.asyncio
async def test_known_neighbourhood_is_offered_when_osm_has_nothing(client, monkeypatch):
    """A dead end is the bug. We hold Lagos neighbourhoods; use them."""

    async def nothing(term, *, limit=6, offline=False):
        return []

    monkeypatch.setattr(opendata, "geocode_async", nothing)

    from app.db.models import LGA, Neighbourhood
    from app.db.session import get_session
    from app.main import app

    async for s in app.dependency_overrides[get_session]():
        s.add(LGA(code="KOS", name="Kosofe"))
        s.add(
            Neighbourhood(
                code="MAG", name="Magodo", lga_code="KOS",
                centroid_lat=6.6181, centroid_lng=3.3778,
            )
        )
        await s.commit()
        break

    r = await client.get("/places/search", params={"q": "16 salako street magodo"})
    assert r.status_code == 200
    body = r.json()
    assert body, "a known neighbourhood must still be offerable"
    assert body[0]["precision"] == "area"
    assert body[0]["resolved"]["area_code"] == "MAG"


@pytest.mark.asyncio
async def test_property_payload_exposes_location_precision(client, session_factory):
    """The screens can only be honest if the API tells them."""
    async with session_factory() as session:
        approx = await identity.identify_or_create(
            session, **CENTROID, address="16 Salako Street", location_approximate=True
        )
        exact = await identity.identify_or_create(
            session,
            lat=6.4531,
            lng=3.4215,
            lga_code="ETI",
            area_code="LEK",
            address="12 Admiralty Way",
        )

    a = await client.get(f"/properties/{approx.property_id}")
    e = await client.get(f"/properties/{exact.property_id}")
    assert a.json()["location_precision"] == "area"
    assert e.json()["location_precision"] == "exact"

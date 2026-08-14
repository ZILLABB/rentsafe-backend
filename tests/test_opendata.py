"""Tests for the open-data import layer.

These never touch the network: the cache is the input, which is the same
property that makes the importer reproducible in CI.
"""

from __future__ import annotations

import pytest

from app.services import opendata


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(opendata, "CACHE_DIR", tmp_path)


def test_offline_without_cache_fails_loudly(monkeypatch):
    """Silently returning nothing would look like 'no transit here'."""
    with pytest.raises(opendata.OpenDataError, match="No cached data"):
        opendata.overpass("[out:json];", key="missing", offline=True)


def test_cached_response_is_used_without_network(monkeypatch):
    opendata.save_cached("osm_lagos_lgas", {"elements": [
        {"tags": {"name": "Eti-Osa"}, "center": {"lat": 6.45, "lon": 3.47}},
        {"tags": {"name": "Ikeja"}, "center": {"lat": 6.6, "lon": 3.35}},
    ]})

    def explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("network was used despite a warm cache")

    monkeypatch.setattr(opendata.httpx, "Client", explode)

    lgas = opendata.lagos_lgas(offline=True)
    assert [x["name"] for x in lgas] == ["Eti-Osa", "Ikeja"]


def test_transit_dedupes_osm_duplicates():
    """Lekki's ferry terminal is mapped twice in OSM, differing only in case."""
    opendata.save_cached(
        "t",
        {
            "elements": [
                {
                    "lat": 6.4400, "lon": 3.4700,
                    "tags": {"amenity": "ferry_terminal", "name": "Alluvia Marine (Lekki 1)"},
                },
                {
                    "lat": 6.4401, "lon": 3.4701,
                    "tags": {"amenity": "ferry_terminal", "name": "alluvia marine (lekki 1)"},
                },
                {
                    "lat": 6.4500, "lon": 3.4800,
                    "tags": {"railway": "station", "name": "Yaba"},
                },
            ]
        },
    )
    found = opendata.transit_near(6.4474, 3.4736, key="t", offline=True)
    names = [f["name"] for f in found]
    assert len(found) == 2, names
    assert "Yaba" in names


def test_transit_reports_real_distances():
    opendata.save_cached(
        "t2",
        {"elements": [{"lat": 6.4474, "lon": 3.4836, "tags": {"railway": "station", "name": "Far"}}]},
    )
    found = opendata.transit_near(6.4474, 3.4736, key="t2", offline=True)
    # 0.01 degrees of longitude at this latitude is roughly 1.1km.
    assert 1000 < found[0]["distance_m"] < 1250


@pytest.mark.parametrize(
    ("elevation", "drainage", "expected"),
    [
        (0.0, 40, "VeryHigh"),    # Ilasan/Jakande — measured at sea level
        (2.0, 300, "VeryHigh"),
        (5.0, 60, "High"),        # Lekki Phase 1 — low and close to drainage
        (5.0, 400, "Moderate"),
        (12.0, 220, "Moderate"),  # Yaba
        (36.0, 500, "Low"),       # Ikeja GRA
    ],
)
def test_flood_banding_follows_elevation(elevation, drainage, expected):
    assert opendata.flood_zone_from_elevation(elevation, drainage) == expected


def test_haversine_matches_known_distance():
    # Lekki Phase 1 to Ikeja GRA is about 19km as the crow flies.
    d = opendata.haversine_m(6.4474, 3.4736, 6.5764, 3.3554)
    assert 18_000 < d < 21_000

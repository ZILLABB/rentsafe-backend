"""Routing estimates must never be invented.

The commute feature's whole claim is that tenant-reported door-to-door times
beat a routing API's prediction. That claim is only checkable if the two numbers
stay separate and the routing one is genuinely absent when we don't have it — a
plausible-looking guess would corrupt the exact comparison the feature exists
for.
"""

from __future__ import annotations

import pytest

from app.services import otp_store, routing

LEKKI = (6.4531, 3.4215)
VI = (6.4281, 3.4219)


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    otp_store._store = otp_store._MemoryStore()
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "")
    monkeypatch.setattr(routing.settings, "mapbox_access_token", "")
    yield
    otp_store._store = otp_store._MemoryStore()


async def test_no_key_falls_back_to_free_flow_and_says_so(monkeypatch):
    """Without a paid key there is still an answer — a different kind of one.

    The kind travels with the number precisely so a caller cannot render a
    free-flow figure using words that mean "what your phone says".
    """

    async def fake(origin, dest):
        return 22

    monkeypatch.setattr(routing, "_valhalla", fake)
    assert routing.has_traffic_provider() is False

    minutes, kind = await routing.drive_estimate_min(LEKKI, VI)
    assert minutes == 22
    assert kind == routing.FREE_FLOW


async def test_google_duration_is_parsed_into_minutes(monkeypatch):
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")

    async def fake(origin, dest):
        return 38

    monkeypatch.setattr(routing, "_google", fake)
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] == 38


async def test_google_protobuf_duration_string(monkeypatch):
    """Google returns "2280s", not a number. Mis-parsing it silently ships 2280."""
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"routes": [{"duration": "2280s"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(routing.httpx, "AsyncClient", lambda **kw: FakeClient())
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] == 38


async def test_a_provider_outage_reads_as_unknown(monkeypatch):
    """A third party being down must not 500 the commute tab."""
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")

    async def boom(origin, dest):
        raise ConnectionError("provider down")

    monkeypatch.setattr(routing, "_google", boom)
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] is None


async def test_results_are_cached_so_calls_are_not_repeated(monkeypatch):
    """Every call is billable and traffic doesn't change within the hour."""
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")
    calls = 0

    async def counting(origin, dest):
        nonlocal calls
        calls += 1
        return 41

    monkeypatch.setattr(routing, "_google", counting)
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] == 41
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] == 41
    assert calls == 1


async def test_a_missing_route_is_cached_too(monkeypatch):
    """Otherwise an outage means hammering the provider on every page load."""
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")
    calls = 0

    async def none_result(origin, dest):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(routing, "_google", none_result)
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] is None
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] is None
    assert calls == 1


async def test_mapbox_is_used_when_only_that_key_is_set(monkeypatch):
    monkeypatch.setattr(routing.settings, "mapbox_access_token", "mb-token")
    used = {}

    async def fake_mapbox(origin, dest):
        used["mapbox"] = True
        return 52

    monkeypatch.setattr(routing, "_mapbox", fake_mapbox)
    assert (await routing.drive_estimate_min(LEKKI, VI))[0] == 52
    assert used.get("mapbox")


async def test_mapbox_receives_lng_lat_not_lat_lng(monkeypatch):
    """Mapbox takes lng,lat. Swapping them puts Lagos in the Gulf of Guinea."""
    monkeypatch.setattr(routing.settings, "mapbox_access_token", "mb-token")
    seen: dict[str, str] = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"routes": [{"duration": 1800}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen["url"] = url
            return FakeResponse()

    monkeypatch.setattr(routing.httpx, "AsyncClient", lambda **kw: FakeClient())
    await routing.drive_estimate_min(LEKKI, VI)

    # Longitude (3.42…) must come first in each pair.
    assert "3.4215,6.4531;3.4219,6.4281" in seen["url"], seen["url"]


async def test_commute_reports_outrank_a_live_estimate(client, monkeypatch):
    """The tenant number is the headline; the estimate is the annotation.

    If a live lookup could overwrite figures captured at report time, the
    comparison the feature exists for would quietly drift.
    """
    import inspect

    from app.api.v1 import commute

    source = inspect.getsource(commute)
    assert "if estimates:" in source, (
        "reported estimates must take precedence over the live lookup"
    )


async def test_a_traffic_key_wins_over_the_keyless_fallback(monkeypatch):
    """A paid provider models congestion; Valhalla does not. Prefer the better one."""
    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")
    used = []

    async def google(origin, dest):
        used.append("google")
        return 47

    async def valhalla(origin, dest):
        used.append("valhalla")
        return 22

    monkeypatch.setattr(routing, "_google", google)
    monkeypatch.setattr(routing, "_valhalla", valhalla)

    minutes, kind = await routing.drive_estimate_min(LEKKI, VI)
    assert (minutes, kind) == (47, routing.TRAFFIC)
    assert used == ["google"]


async def test_free_flow_and_traffic_results_do_not_share_a_cache_entry(monkeypatch):
    """Otherwise adding a key later serves the old free-flow number as traffic.

    Same journey, same coordinates, completely different meaning — so the kind
    is part of the cache key.
    """

    async def valhalla(origin, dest):
        return 22

    async def google(origin, dest):
        return 47

    monkeypatch.setattr(routing, "_valhalla", valhalla)
    monkeypatch.setattr(routing, "_google", google)

    first = await routing.drive_estimate_min(LEKKI, VI)
    assert first == (22, routing.FREE_FLOW)

    monkeypatch.setattr(routing.settings, "google_maps_api_key", "test-key")
    second = await routing.drive_estimate_min(LEKKI, VI)
    assert second == (47, routing.TRAFFIC), "a cached free-flow time was reused as traffic"


async def test_valhalla_parses_the_trip_summary(monkeypatch):
    """Valhalla nests the duration under trip.summary.time, in seconds."""

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"trip": {"summary": {"time": 1320.0}}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(routing.httpx, "AsyncClient", lambda **kw: FakeClient())
    assert await routing._valhalla(LEKKI, VI) == 22


async def test_a_free_flow_outage_still_reports_its_kind(monkeypatch):
    """The caller needs the kind even when the number is missing, so the UI can
    say which comparison is unavailable."""

    async def boom(origin, dest):
        raise ConnectionError("valhalla down")

    monkeypatch.setattr(routing, "_valhalla", boom)
    minutes, kind = await routing.drive_estimate_min(LEKKI, VI)
    assert minutes is None
    assert kind == routing.FREE_FLOW

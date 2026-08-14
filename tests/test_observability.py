"""Request IDs, structured logs and metrics.

None of this existed: a failure was visible only if a user reported it, and no
log line could be tied back to the request that produced it.

The privacy assertions here matter as much as the functional ones. Address
search carries a user's home address in `q`, and review submissions carry text
tenants would not put their name to. Log lines get copied to more places than a
database does, so a query string in an access log is a leak.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core import observability
from app.main import app


async def test_every_response_carries_a_request_id(client):
    r = await client.get("/properties")
    assert r.headers.get("X-Request-ID")
    assert len(r.headers["X-Request-ID"]) <= 64


async def test_an_inbound_request_id_is_reused(client):
    """A trace has to survive the proxy hop, or correlation is pointless."""
    r = await client.get("/properties", headers={"X-Request-ID": "abc123trace"})
    assert r.headers["X-Request-ID"] == "abc123trace"


async def test_a_hostile_request_id_is_sanitised(client):
    """The value is echoed into a header and a log line; neither may be injectable."""
    r = await client.get(
        "/properties",
        headers={"X-Request-ID": "bad\r\nX-Evil: 1"},
    )
    got = r.headers["X-Request-ID"]
    assert "\r" not in got and "\n" not in got
    assert "X-Evil" not in r.headers


async def test_a_long_request_id_is_capped(client):
    r = await client.get("/properties", headers={"X-Request-ID": "z" * 500})
    assert len(r.headers["X-Request-ID"]) == 64


def test_metrics_render_in_prometheus_format():
    m = observability.Metrics()
    m.observe("GET", "/properties", 200, 0.02)
    m.observe("GET", "/properties", 200, 3.0)
    m.observe("GET", "/properties", 500, 0.01)
    m.record_exception("ValueError")
    out = m.render()

    assert 'rentsafe_requests_total{method="GET",route="/properties",status="200"} 2' in out
    assert 'rentsafe_requests_total{method="GET",route="/properties",status="500"} 1' in out
    assert 'rentsafe_exceptions_total{type="ValueError"} 1' in out
    # Histogram buckets are cumulative, and the +Inf bucket holds every sample.
    assert 'le="+Inf"} 3' in out
    assert 'rentsafe_request_seconds_count{method="GET",route="/properties"} 3' in out


def test_latency_buckets_are_cumulative():
    m = observability.Metrics()
    for _ in range(3):
        m.observe("GET", "/x", 200, 0.02)  # falls in the 0.05 bucket
    out = m.render()
    # Nothing at or below 0.01, all three at or below 0.05 and every bucket above.
    assert 'le="0.01"} 0' in out
    assert 'le="0.05"} 3' in out
    assert 'le="1.0"} 3' in out


async def test_metrics_endpoint_serves_scrapes(client):
    await client.get("/properties")
    r = await client.get("http://test/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "rentsafe_requests_total" in r.text


async def test_metrics_label_routes_not_concrete_paths(client):
    """One time series per property would blow up a scrape."""
    await client.get("/properties/ETI-LEK-7F3A2B-0041")
    r = await client.get("http://test/metrics")
    assert "{property_id}" in r.text
    assert "ETI-LEK-7F3A2B-0041" not in r.text


async def test_unmatched_paths_collapse_to_one_label(client):
    """Otherwise a 404 scanner mints a time series per URL it guesses."""
    for i in range(3):
        await client.get(f"/no/such/path/{i}")
    r = await client.get("http://test/metrics")
    assert "<other>" in r.text
    assert "/no/such/path/1" not in r.text


async def test_access_logs_never_carry_the_query_string(client, caplog):
    """`q` on address search is the user's home address."""
    with caplog.at_level(logging.INFO, logger="app.access"):
        await client.get("/places/search?q=16 salako street magodo")

    ours = [r for r in caplog.records if r.name.startswith("app")]
    blob = json.dumps([r.__dict__ for r in ours], default=str)
    assert "salako" not in blob.lower(), "the access log leaked the search term"
    # The label is the full templated path, API prefix included.
    assert any(
        str(getattr(r, "route", "")).endswith("/places/search") for r in ours
    )


def test_url_logging_clients_are_pinned_quiet():
    """httpx logs full request URLs at INFO — and we geocode the user's address.

    Production sits at WARNING anyway, so this guards the *next* person: raising
    the root level to chase an unrelated bug must not start writing tenants'
    home addresses to the logs.
    """
    observability.configure_logging(debug=True)
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        observability.configure_logging(debug=True)


def test_json_formatter_emits_one_object_per_line():
    record = logging.LogRecord(
        "app.access", logging.INFO, __file__, 1, "request", None, None
    )
    record.request_id = "trace-1"
    record.status = 200
    record.duration_ms = 12.5

    line = observability.JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["request_id"] == "trace-1"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.5
    assert payload["message"] == "request"


def test_request_id_appears_on_log_lines_from_anywhere():
    """The whole point: a line emitted deep in a service is correlatable."""
    record = logging.LogRecord("app.services.x", logging.INFO, __file__, 1, "hi", None, None)
    token = observability.request_id_var.set("deep-trace")
    try:
        assert observability.RequestIdFilter().filter(record) is True
        assert record.request_id == "deep-trace"
    finally:
        observability.request_id_var.reset(token)


def test_observe_is_the_outermost_middleware():
    """It must wrap the others, or failures inside them go uncounted."""
    names = [
        getattr(m.kwargs.get("dispatch", None), "__name__", m.cls.__name__)
        for m in app.user_middleware
    ]
    assert names[0] == "observe", names


async def test_a_failing_request_is_counted_and_logged(client, monkeypatch, caplog):
    """A 500 that leaves no trace is the failure mode this whole module exists for."""
    from app.api.v1 import properties

    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    # Patched inside the handler rather than replacing the handler itself:
    # FastAPI captured the original function at registration time, so
    # reassigning the module attribute would have no effect and the test would
    # pass without exercising anything.
    # `_with_relations` runs before the query, so this fires even though the
    # test database has no properties to serialise.
    monkeypatch.setattr(properties, "_with_relations", boom)

    before = observability.metrics.exceptions.get("RuntimeError", 0)
    with caplog.at_level(logging.ERROR, logger="app.access"), pytest.raises(RuntimeError):
        await client.get("/properties")

    assert observability.metrics.exceptions.get("RuntimeError", 0) == before + 1
    assert any("request failed" in r.getMessage() for r in caplog.records)

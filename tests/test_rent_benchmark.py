"""The official rent benchmark.

Every rent figure in this app comes from tenants, which is the point — and it
also means a tenant told "your rent rose 40%" has nothing to judge that
against. This is the one number sourced from outside, so the rules about it are
strict: it must be the real rent series, it must never be presented as Lagos
data when it is national, and it must be absent rather than guessed.
"""

from __future__ import annotations

import pytest

from app.services import opendata


def test_year_on_year_needs_twelve_months_of_history():
    """A year-on-year figure computed from less than a year is not one."""
    series = [{"year": 2026, "month": m, "index": 100 + m} for m in range(1, 7)]
    out = opendata.year_on_year(series)
    assert all(r["yoy_pct"] is None for r in out)


def test_year_on_year_is_computed_against_the_same_month():
    """Comparing against the previous *month* would report noise as inflation."""
    series = [
        {"year": 2025, "month": 6, "index": 100.0},
        {"year": 2026, "month": 5, "index": 200.0},  # decoy: wrong month
        {"year": 2026, "month": 6, "index": 115.0},
    ]
    out = {(r["year"], r["month"]): r["yoy_pct"] for r in opendata.year_on_year(series)}
    assert out[(2026, 6)] == pytest.approx(15.0)


def test_a_zero_base_does_not_divide_by_zero():
    series = [
        {"year": 2025, "month": 1, "index": 0.0},
        {"year": 2026, "month": 1, "index": 120.0},
    ]
    out = opendata.year_on_year(series)
    assert out[-1]["yoy_pct"] is None


async def test_the_endpoint_reports_absence_rather_than_a_guess(client):
    """With no benchmark imported, the figure is null — never a placeholder."""
    r = await client.get("/neighbourhoods/rent-benchmark")
    assert r.status_code == 200
    body = r.json()
    assert body["yoy_pct"] is None
    assert body["period_year"] is None


async def test_the_endpoint_says_the_figure_is_national(client, session_factory):
    """NBS does not publish the rent index by state.

    Letting a reader take it for a Lagos number would be exactly the quiet
    overclaim this app exists to avoid, so the scope travels with the value.
    """
    from app.db.models import RentBenchmark

    async with session_factory() as s:
        s.add(
            RentBenchmark(
                scope="national",
                period_year=2026,
                period_month=6,
                index_value=148.2452,
                yoy_pct=14.81,
            )
        )
        await s.commit()

    body = (await client.get("/neighbourhoods/rent-benchmark")).json()
    assert body["scope"] == "national"
    assert body["yoy_pct"] == pytest.approx(14.8)
    assert body["period_year"] == 2026 and body["period_month"] == 6
    # The source is named so a reader can check the number themselves.
    assert "NBS" in body["source"] and body["url"].startswith("https://")


async def test_only_periods_with_a_real_change_are_offered(client, session_factory):
    """The newest month has no year-on-year figure until its base year exists."""
    from app.db.models import RentBenchmark

    async with session_factory() as s:
        s.add(
            RentBenchmark(
                scope="national", period_year=2026, period_month=6,
                index_value=148.0, yoy_pct=14.8,
            )
        )
        s.add(
            RentBenchmark(
                scope="national", period_year=2026, period_month=7,
                index_value=150.0, yoy_pct=None,
            )
        )
        await s.commit()

    body = (await client.get("/neighbourhoods/rent-benchmark")).json()
    # July has no yoy yet, so June is the most recent usable figure.
    assert body["period_month"] == 6
    assert body["yoy_pct"] == pytest.approx(14.8)


def test_the_rent_index_is_the_rent_series_not_housing_and_utilities():
    """NBS publishes both; only one of them is about rent.

    "Housing, Water, Electricity, Gas And Other Fuels" is the division people
    usually quote, and it moves with fuel prices. The column this reads is
    HOUSING (RENT) INDEX, weight 4.23.
    """
    import inspect

    source = inspect.getsource(opendata.nbs_rent_index)
    assert '"housing" in str(cell).lower() and "rent" in str(cell).lower()' in source


def test_the_release_url_is_discovered_not_pinned():
    """The download id changes monthly.

    Pinning it means the importer silently keeps re-importing one stale release
    and quoting last year's inflation as current.
    """
    import inspect

    source = inspect.getsource(opendata._latest_cpi_zip_url)
    assert "download/" in source and "findall" in source

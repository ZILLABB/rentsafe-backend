"""The fee checker.

This is the only screen that answers without any reviews of the building, which
is what makes it useful with nine properties in the database. It is also the
question Lagos tenants get burned on: agent and agreement fees are quoted as a
share of annual rent, and the gap between customary and charged is where
overcharging lives.

Two things must never happen here:

* calling a normal fee excessive, which would cost a tenant an agent for
  nothing; and
* stating a customary convention as if it were law. What an agent may charge is
  for LASRERA and the tenancy agreement, not for us to assert.
"""

from __future__ import annotations

import pytest

from tests.test_api_places import lagos_areas  # noqa: F401 — fixture used by name

RENT = 130_000_000  # ₦1.3m/yr in kobo


async def _check(client, **params):
    r = await client.get("/fees/check", params={"rent_kobo": RENT, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_needs_no_account(client):
    """Someone sent a number on WhatsApp can check it before signing up."""
    r = await client.get("/fees/check", params={"rent_kobo": RENT})
    assert r.status_code == 200


async def test_a_customary_fee_reads_as_typical(client, lagos_areas):
    data = await _check(client, agreement_fee_kobo=RENT // 10)  # exactly 10%
    line = next(x for x in data["lines"] if x["label"] == "Agreement fee")
    assert line["pct_of_rent"] == 10.0
    assert line["verdict"] == "typical"


async def test_a_double_fee_reads_as_very_high(client, lagos_areas):
    data = await _check(client, agent_fee_kobo=RENT // 5)  # 20%
    line = next(x for x in data["lines"] if x["label"] == "Agent fee")
    assert line["pct_of_rent"] == 20.0
    assert line["verdict"] == "very_high"


async def test_a_slightly_high_fee_is_not_called_excessive(client, lagos_areas):
    """Overreacting costs a tenant an agent over nothing."""
    data = await _check(client, agreement_fee_kobo=int(RENT * 0.105))
    line = next(x for x in data["lines"] if x["label"] == "Agreement fee")
    assert line["verdict"] == "typical"


async def test_the_caution_deposit_is_never_judged(client, lagos_areas):
    """It is a refundable deposit, not a fee. Comparing it to a fee benchmark
    would be comparing two different kinds of money."""
    data = await _check(client, caution_fee_kobo=RENT)  # 100% of rent
    line = next(x for x in data["lines"] if x["label"] == "Caution deposit")
    assert line["verdict"] == "typical"
    assert "refundable" in line["note"].lower()


async def test_nothing_is_stated_as_a_legal_limit(client, lagos_areas):
    """Customary figures are conventions, not statute."""
    data = await _check(
        client, agent_fee_kobo=RENT // 2, agreement_fee_kobo=RENT // 2
    )
    blob = " ".join(x["note"] for x in data["lines"]) + data["summary"]
    for word in ("illegal", "unlawful", "against the law", "prohibited"):
        assert word not in blob.lower(), f"the checker asserted legality: {word!r}"


async def test_the_total_includes_the_rent_itself(client, lagos_areas):
    """The number a tenant actually has to find before moving in."""
    data = await _check(
        client,
        agent_fee_kobo=RENT // 10,
        agreement_fee_kobo=RENT // 10,
        caution_fee_kobo=RENT // 10,
    )
    assert data["total_upfront_kobo"] == RENT + 3 * (RENT // 10)
    assert data["total_as_pct_of_rent"] == pytest.approx(130.0, abs=0.5)


async def test_no_fees_given_produces_no_verdicts(client, lagos_areas):
    data = await _check(client)
    assert data["lines"] == []
    assert "Enter the fees" in data["summary"]


async def test_a_zero_rent_is_rejected(client):
    """Everything here is a percentage of rent, so zero would divide by zero."""
    r = await client.get("/fees/check", params={"rent_kobo": 0})
    assert r.status_code == 422


async def test_an_unknown_area_falls_back_rather_than_failing(client, lagos_areas):
    data = await _check(client, area="NOSUCHAREA", agent_fee_kobo=RENT // 10)
    assert data["area_code"] is None
    # Still answers, using the city-wide or customary benchmark.
    assert data["lines"]


async def test_the_benchmark_is_reported_so_the_number_is_checkable(
    client, lagos_areas
):
    """A verdict without its basis is just an assertion."""
    data = await _check(client, agent_fee_kobo=RENT // 10)
    line = next(x for x in data["lines"] if x["label"] == "Agent fee")
    assert line["benchmark_pct"] > 0
    assert str(int(line["benchmark_pct"])) in line["note"]

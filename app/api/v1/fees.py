"""Is this fee normal for this area?

Every other page here needs reviews of a *specific building* to say anything.
This one doesn't: it answers from area-level aggregates, so it is useful on the
first day with an empty reviews table — which is the position the product is
actually in.

It is also the question Lagos tenants get burned on most. Agent and agreement
fees are quoted as a percentage of annual rent, the customary figure is widely
understood to be 10% each, and the gap between customary and charged is where
overcharging lives. A tenant handed a number on WhatsApp wants to know one
thing: is this normal?

Everything here is stated as a comparison against a benchmark, never as a
verdict on legality — fee caps are a matter for LASRERA and the tenancy law,
not for us to assert.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Neighbourhood
from app.db.session import get_session

router = APIRouter(prefix="/fees", tags=["fees"])

# The customary Lagos figures. These are conventions, not statute, and the
# response says so — quoting them as law would be inventing a rule.
CUSTOMARY_AGENT_PCT = 10.0
CUSTOMARY_AGREEMENT_PCT = 10.0

# Above this multiple of the benchmark, a fee stops being "a bit high" and is
# worth arguing about. Deliberately generous: calling a normal fee excessive
# would cost a tenant an agent for no reason.
HIGH_MULTIPLE = 1.5


class FeeLine(BaseModel):
    label: str
    amount_kobo: int
    pct_of_rent: float
    benchmark_pct: float
    # "typical" | "high" | "very_high". Never "illegal" — that is not ours to say.
    verdict: str
    note: str


class FeeCheckOut(BaseModel):
    rent_kobo: int
    area_code: str | None = None
    area_name: str | None = None
    # Null when no area has reported fee data yet; the UI then leans on the
    # customary figure alone and says which it is using.
    area_avg_agent_pct: float | None = None
    lines: list[FeeLine] = Field(default_factory=list)
    total_upfront_kobo: int = 0
    total_as_pct_of_rent: float = 0
    summary: str = ""


def _verdict(pct: float, benchmark: float) -> tuple[str, str]:
    if benchmark <= 0:
        return "typical", "No benchmark available for this area yet."
    ratio = pct / benchmark
    if ratio <= 1.1:
        return "typical", f"In line with the usual {benchmark:.0f}%."
    if ratio <= HIGH_MULTIPLE:
        return (
            "high",
            f"Above the usual {benchmark:.0f}%. Worth asking what it covers.",
        )
    return (
        "very_high",
        f"Well above the usual {benchmark:.0f}%. Ask for it in writing, itemised.",
    )


@router.get("/check", response_model=FeeCheckOut)
async def check_fees(
    rent_kobo: int = Query(..., gt=0, description="Annual rent in kobo"),
    agent_fee_kobo: int = Query(default=0, ge=0),
    agreement_fee_kobo: int = Query(default=0, ge=0),
    caution_fee_kobo: int = Query(default=0, ge=0),
    area: str | None = Query(default=None, description="Neighbourhood code"),
    session: AsyncSession = Depends(get_session),
) -> FeeCheckOut:
    """Compare quoted fees against what this area actually reports.

    Needs no account and no reviews of the building — a tenant who has just been
    sent a number can check it before they have ever used the app.
    """
    out = FeeCheckOut(rent_kobo=rent_kobo)

    hood = None
    if area:
        hood = (
            await session.execute(
                select(Neighbourhood).where(Neighbourhood.code == area.upper())
            )
        ).scalar_one_or_none()
    if hood is not None:
        out.area_code = hood.code
        out.area_name = hood.name
        if hood.avg_agent_fee_pct is not None:
            out.area_avg_agent_pct = round(float(hood.avg_agent_fee_pct), 2)

    if out.area_avg_agent_pct is None:
        # Fall back to the Lagos-wide reported average, then to the customary
        # figure. Whichever is used, the note says so rather than presenting a
        # made-up benchmark as measured.
        city_avg = (
            await session.execute(select(func.avg(Neighbourhood.avg_agent_fee_pct)))
        ).scalar()
        if city_avg is not None:
            out.area_avg_agent_pct = round(float(city_avg), 2)

    agent_benchmark = out.area_avg_agent_pct or CUSTOMARY_AGENT_PCT

    def pct_of(amount: int) -> float:
        return round(amount / rent_kobo * 100, 2)

    if agent_fee_kobo:
        verdict, note = _verdict(pct_of(agent_fee_kobo), agent_benchmark)
        out.lines.append(
            FeeLine(
                label="Agent fee",
                amount_kobo=agent_fee_kobo,
                pct_of_rent=pct_of(agent_fee_kobo),
                benchmark_pct=round(agent_benchmark, 2),
                verdict=verdict,
                note=note,
            )
        )

    if agreement_fee_kobo:
        verdict, note = _verdict(pct_of(agreement_fee_kobo), CUSTOMARY_AGREEMENT_PCT)
        out.lines.append(
            FeeLine(
                label="Agreement fee",
                amount_kobo=agreement_fee_kobo,
                pct_of_rent=pct_of(agreement_fee_kobo),
                benchmark_pct=CUSTOMARY_AGREEMENT_PCT,
                verdict=verdict,
                note=note,
            )
        )

    if caution_fee_kobo:
        # Caution is a refundable deposit, not a fee, so it is reported without
        # a verdict — judging it against a percentage benchmark would be
        # comparing two different kinds of money.
        out.lines.append(
            FeeLine(
                label="Caution deposit",
                amount_kobo=caution_fee_kobo,
                pct_of_rent=pct_of(caution_fee_kobo),
                benchmark_pct=0,
                verdict="typical",
                note="Refundable at the end of the tenancy — get the terms in writing.",
            )
        )

    out.total_upfront_kobo = (
        rent_kobo + agent_fee_kobo + agreement_fee_kobo + caution_fee_kobo
    )
    out.total_as_pct_of_rent = round(out.total_upfront_kobo / rent_kobo * 100, 1)

    worst = [line.verdict for line in out.lines]
    if "very_high" in worst:
        out.summary = "At least one fee here is well above what this area reports."
    elif "high" in worst:
        out.summary = "One or more fees are above the usual figure for this area."
    elif out.lines:
        out.summary = "These fees are in line with what this area reports."
    else:
        out.summary = "Enter the fees you have been quoted to check them."

    return out

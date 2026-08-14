"""Property score aggregation — Section III.

Pure functions, no DB. The service layer pulls reviews from Postgres and feeds
plain dataclasses in here so the weighting logic is independently testable.

Two weights combine multiplicatively per review:

  * Recency weight (newer reviews count more):
        <=12 months -> 1.00
        <=24 months -> 0.75
        <=36 months -> 0.50
        else        -> 0.25
  * Verification weight: verified-tenant reviews count 1.5x unverified ones.

Reviews dated before a management-change cutoff are excluded from the *current*
aggregate ("Under previous management") but remain visible historically.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

VERIFIED_MULTIPLIER = 1.5

# The 10 structured dimensions (Section III), in display order.
DIMENSIONS = (
    "landlord",
    "agent",
    "property",
    "water",
    "power",
    "security",
    "noise",
    "flooding",
    "neighbourhood",
    "value",
)


@dataclass
class ReviewScore:
    """The slice of a review that aggregation needs."""

    ratings: dict[str, int]  # dimension -> 1..5
    created_at: dt.date
    verified: bool


def recency_weight(age_months: int) -> float:
    if age_months <= 12:
        return 1.0
    if age_months <= 24:
        return 0.75
    if age_months <= 36:
        return 0.5
    return 0.25


def months_between(earlier: dt.date, later: dt.date) -> int:
    """Whole months from ``earlier`` to ``later`` (never negative)."""
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(0, months)


def review_aggregate(ratings: dict[str, int]) -> float:
    """Mean of the supplied dimensions present in ``ratings`` (ignores missing)."""
    vals = [ratings[d] for d in DIMENSIONS if ratings.get(d) is not None]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def review_weight(r: ReviewScore, as_of: dt.date) -> float:
    age = months_between(r.created_at, as_of)
    w = recency_weight(age)
    if r.verified:
        w *= VERIFIED_MULTIPLIER
    return w


@dataclass
class AggregateResult:
    overall: float | None          # weighted aggregate, rounded to 2dp
    per_dimension: dict[str, float]  # weighted mean per dimension, 2dp
    counted: int                   # reviews included
    excluded_prior_mgmt: int       # reviews dropped by the cutoff


def aggregate_property(
    reviews: list[ReviewScore],
    *,
    as_of: dt.date | None = None,
    management_change: dt.date | None = None,
) -> AggregateResult:
    """Weighted aggregate across reviews (Section III)."""
    # UTC, not local: "today" on a server in another timezone would shift every
    # recency weight by a day at the boundary.
    as_of = as_of or dt.datetime.now(dt.UTC).date()

    excluded = 0
    included: list[ReviewScore] = []
    for r in reviews:
        if management_change and r.created_at < management_change:
            excluded += 1
            continue
        included.append(r)

    if not included:
        return AggregateResult(None, {}, 0, excluded)

    total_w = 0.0
    overall_acc = 0.0
    dim_acc: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    dim_w: dict[str, float] = {d: 0.0 for d in DIMENSIONS}

    for r in included:
        w = review_weight(r, as_of)
        total_w += w
        overall_acc += w * review_aggregate(r.ratings)
        for d in DIMENSIONS:
            v = r.ratings.get(d)
            if v is not None:
                dim_acc[d] += w * v
                dim_w[d] += w

    overall = round(overall_acc / total_w, 2) if total_w else None
    per_dimension = {
        d: round(dim_acc[d] / dim_w[d], 2) for d in DIMENSIONS if dim_w[d] > 0
    }
    return AggregateResult(overall, per_dimension, len(included), excluded)

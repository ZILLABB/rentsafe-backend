"""Review service — submission, moderation routing, and score recompute.

Ties the pure cores (scoring, moderation) to Postgres. On submit we:
  1. Resolve the PropertyID -> internal property row.
  2. Run the automated moderation pre-check (Section III step 2/3).
  3. Persist the review (status 'flagged' -> human queue, else 'pending').
  4. Recompute and cache the property's aggregate scores.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import moderation, scoring
from app.db.models import Agent, Property, Review, User, as_utc
from app.schemas.review import ReviewCreate, ReviewUpdate

_RATING_COLS = {
    "landlord": Review.rating_landlord,
    "agent": Review.rating_agent,
    "property": Review.rating_property,
    "water": Review.rating_water,
    "power": Review.rating_power,
    "security": Review.rating_security,
    "noise": Review.rating_noise,
    "flooding": Review.rating_flooding,
    "neighbourhood": Review.rating_neighbourhood,
    "value": Review.rating_value,
}


async def _get_property(session: AsyncSession, property_id: str) -> Property | None:
    return (
        await session.execute(
            select(Property).where(Property.property_id == property_id.upper())
        )
    ).scalar_one_or_none()


async def _resolve_agent(session: AsyncSession, name: str | None) -> int | None:
    if not name:
        return None
    norm = name.strip().lower()
    agent = (
        await session.execute(select(Agent).where(Agent.name_normalised == norm))
    ).scalar_one_or_none()
    if agent is None:
        agent = Agent(name=name.strip(), name_normalised=norm)
        session.add(agent)
        await session.flush()
    return agent.id


async def _reviews_last_24h(session: AsyncSession, user_id: int) -> int:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
    return (
        await session.execute(
            select(func.count())
            .select_from(Review)
            .where(Review.user_id == user_id, Review.created_at >= since)
        )
    ).scalar_one()


def _normalise_text(*parts: str | None) -> str:
    """Collapse whitespace and case so trivial edits don't defeat the check."""
    joined = " ".join(p for p in parts if p)
    return " ".join(joined.lower().split())


async def _is_duplicate_text(
    session: AsyncSession, property_pk: int, payload: ReviewCreate
) -> bool:
    """True when this property already carries a review with the same body.

    Catches the copy-paste review-bombing pattern — the same text submitted
    against one property from several accounts.
    """
    candidate = _normalise_text(payload.text_positives, payload.text_warnings)
    if len(candidate) < 40:  # too short to be a meaningful match
        return False
    rows = (
        await session.execute(
            select(Review.text_positives, Review.text_warnings).where(
                Review.property_id == property_pk,
                Review.moderation_status != "rejected",
            )
        )
    ).all()
    return any(_normalise_text(r[0], r[1]) == candidate for r in rows)


async def submit_review(
    session: AsyncSession, user: User, payload: ReviewCreate
) -> tuple[Review, moderation.ModerationResult]:
    prop = await _get_property(session, payload.property_id)
    if prop is None:
        raise LookupError("property not found")

    recent = await _reviews_last_24h(session, user.id)
    duplicate = await _is_duplicate_text(session, prop.id, payload)
    verdict = moderation.check_submission(
        texts=[payload.text_positives, payload.text_warnings, payload.text_negotiation_tips],
        user_reviews_last_24h=recent,
        duplicate_of_existing=duplicate,
    )

    agent_id = await _resolve_agent(session, payload.agent_name)

    review = Review(
        property_id=prop.id,
        user_id=user.id,
        agent_id=agent_id,
        tenancy_start=payload.tenancy_start,
        tenancy_end=payload.tenancy_end,
        still_living=payload.still_living,
        rent_amount_kobo=payload.rent_amount_kobo,
        rent_period=payload.rent_period,
        agent_fee_kobo=payload.agent_fee_kobo,
        caution_fee_kobo=payload.caution_fee_kobo,
        agreement_fee_kobo=payload.agreement_fee_kobo,
        departure_reason=payload.departure_reason,
        rating_landlord=payload.ratings.landlord,
        rating_agent=payload.ratings.agent,
        rating_property=payload.ratings.property,
        rating_water=payload.ratings.water,
        rating_power=payload.ratings.power,
        rating_security=payload.ratings.security,
        rating_noise=payload.ratings.noise,
        rating_flooding=payload.ratings.flooding,
        rating_neighbourhood=payload.ratings.neighbourhood,
        rating_value=payload.ratings.value,
        text_positives=payload.text_positives,
        text_warnings=payload.text_warnings,
        text_negotiation_tips=payload.text_negotiation_tips,
        is_anonymous=payload.is_anonymous,
        display_name=user.display_name,
        moderation_status="flagged" if verdict.needs_human else "pending",
        flag_reasons=verdict.reasons or None,
    )
    session.add(review)

    user.review_count = (user.review_count or 0) + 1

    # Flush (don't commit) so the review has an id and participates in the
    # recompute below, then commit both together — a failed recompute must not
    # leave a persisted review sitting behind stale aggregates.
    await session.flush()
    await recompute_property_scores(session, prop.id, commit=False)
    await session.commit()
    await session.refresh(review)
    return review, verdict


async def recompute_property_scores(
    session: AsyncSession, property_pk: int, *, commit: bool = True
) -> None:
    """Recalculate and cache a property's aggregate scores (Section III).

    Only approved reviews count. Un-moderated content is invisible to the public
    (see ``api/v1/reviews.py``), so letting it move the score would leak the
    thing we withheld — and would let a single unreviewed submission swing a
    property's public rating.

    Pass ``commit=False`` to enlist in the caller's transaction.
    """
    rows = (
        await session.execute(
            select(Review).where(
                Review.property_id == property_pk,
                Review.moderation_status == "approved",
            )
        )
    ).scalars().all()

    scores = [
        scoring.ReviewScore(
            ratings={dim: getattr(r, f"rating_{dim}") for dim in scoring.DIMENSIONS},
            created_at=(
                r.created_at.date()
                if r.created_at
                else dt.datetime.now(dt.UTC).date()
            ),
            verified=r.verified_tenant,
        )
        for r in rows
    ]
    result = scoring.aggregate_property(scores)

    prop = (
        await session.execute(select(Property).where(Property.id == property_pk))
    ).scalar_one()
    prop.avg_rating = result.overall
    # The weighted per-dimension means are cached here so the API never has to
    # recompute them with a different (unweighted) formula.
    prop.rating_breakdown = {d: result.per_dimension.get(d, 0.0) for d in scoring.DIMENSIONS}
    prop.total_reviews = len(rows)
    prop.verified_reviews = sum(1 for r in rows if r.verified_tenant)
    latest = max(
        (r for r in rows if r.rent_amount_kobo), default=None, key=lambda r: r.tenancy_start
    )
    if latest:
        prop.latest_rent_kobo = latest.rent_amount_kobo
    if commit:
        await session.commit()


# How long an author may amend or withdraw their own review.
#
# A window rather than "forever": once other tenants have read a review and a
# landlord has replied to it, silently rewriting the record underneath them is
# its own dishonesty. A window rather than "never": the terms make the author
# personally liable for a false statement of fact about a named landlord, and a
# platform that offers no way to correct one is indefensible.
EDIT_WINDOW = dt.timedelta(hours=48)


def edit_window_remaining(review: Review) -> dt.timedelta:
    """Time left to amend. Zero or negative means the window has closed."""
    created = as_utc(review.created_at) or dt.datetime.now(dt.UTC)
    return (created + EDIT_WINDOW) - dt.datetime.now(dt.UTC)


async def update_review(
    session: AsyncSession, review: Review, payload: ReviewUpdate
) -> tuple[Review, moderation.ModerationResult]:
    """Apply an author's amendment and send it back through moderation.

    Edited text is re-checked and the review returns to the queue. Skipping that
    would make the edit window a way to launder an accusation past moderation:
    publish something bland, then rewrite it once approved.
    """
    if payload.ratings is not None:
        for dim in (
            "landlord", "agent", "property", "water", "power",
            "security", "noise", "flooding", "neighbourhood", "value",
        ):
            setattr(review, f"rating_{dim}", getattr(payload.ratings, dim))

    for field in ("text_positives", "text_warnings", "text_negotiation_tips"):
        value = getattr(payload, field)
        if value is not None:
            setattr(review, field, value)
    if payload.rent_amount_kobo is not None:
        review.rent_amount_kobo = payload.rent_amount_kobo
    if payload.is_anonymous is not None:
        review.is_anonymous = payload.is_anonymous

    verdict = moderation.check_submission(
        texts=[review.text_positives, review.text_warnings, review.text_negotiation_tips],
        user_reviews_last_24h=0,  # an edit is not a new submission
        duplicate_of_existing=False,
    )
    review.moderation_status = "flagged" if verdict.needs_human else "pending"
    review.flag_reasons = verdict.reasons or None
    review.edited_at = dt.datetime.now(dt.UTC)

    await session.flush()
    # The review has left "approved", so the property's aggregates must drop it.
    await recompute_property_scores(session, review.property_id, commit=False)
    await session.commit()
    await session.refresh(review)
    return review, verdict


async def delete_review(session: AsyncSession, review: Review, user: User) -> None:
    """Withdraw a review inside the edit window.

    A hard delete, unlike account closure which anonymises: this is the author
    retracting one statement they chose to make, not a data-subject request that
    a landlord could pressure them into making wholesale.
    """
    property_pk = review.property_id
    await session.delete(review)
    user.review_count = max(0, (user.review_count or 1) - 1)
    await session.flush()
    await recompute_property_scores(session, property_pk, commit=False)
    await session.commit()

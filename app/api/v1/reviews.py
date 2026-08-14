"""Review endpoints (Section XII /reviews)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user
from app.core import scoring
from app.db.models import Agent, ModerationAction, Property, Review, User
from app.db.session import get_session
from app.schemas.review import (
    OwnerResponse,
    Ratings,
    ReviewCreate,
    ReviewOut,
    ReviewSubmitResponse,
    ReviewUpdate,
)
from app.services import reviews as review_service

router = APIRouter(tags=["reviews"])


def _to_out(r: Review, property_code: str, agent_name: str | None = None) -> ReviewOut:
    ratings = Ratings(
        landlord=r.rating_landlord,
        agent=r.rating_agent,
        property=r.rating_property,
        water=r.rating_water,
        power=r.rating_power,
        security=r.rating_security,
        noise=r.rating_noise,
        flooding=r.rating_flooding,
        neighbourhood=r.rating_neighbourhood,
        value=r.rating_value,
    )
    return ReviewOut(
        id=r.id,
        property_id=property_code,
        tenancy_start=r.tenancy_start,
        tenancy_end=r.tenancy_end,
        still_living=r.still_living,
        rent_amount_kobo=r.rent_amount_kobo,
        agent_fee_kobo=r.agent_fee_kobo,
        agent_name=agent_name,
        ratings=ratings,
        aggregate=round(scoring.review_aggregate(ratings.model_dump()), 2),
        verification_tier=r.verification_tier,
        verified_tenant=r.verified_tenant,
        is_anonymous=r.is_anonymous,
        display_name=(
            "Anonymous tenant" if r.is_anonymous else (r.display_name or "Tenant")
        ),
        text_positives=r.text_positives,
        text_warnings=r.text_warnings,
        owner_response=(
            OwnerResponse(from_=r.owner_response_from or "landlord", text=r.owner_response)
            if r.owner_response
            else None
        ),
        moderation_status=r.moderation_status,
        edited_at=r.edited_at,
        created_at=r.created_at,
    )


@router.post("/reviews", response_model=ReviewSubmitResponse, status_code=201)
async def submit_review(
    payload: ReviewCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewSubmitResponse:
    try:
        review, verdict = await review_service.submit_review(session, user, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="property not found") from exc

    msg = (
        "Your review needs a quick manual check and will appear shortly."
        if verdict.needs_human
        else "Published — pending verification."
    )
    return ReviewSubmitResponse(
        review_id=review.id,
        moderation_status=review.moderation_status,
        flagged_reasons=verdict.reasons,
        message=msg,
    )


@router.get("/reviews/mine", response_model=list[ReviewOut])
async def list_my_reviews(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReviewOut]:
    """The signed-in user's own reviews, whatever their moderation status."""
    rows = (
        await session.execute(
            select(Review, Property.property_id, Agent.company_name, Agent.name)
            .join(Property, Property.id == Review.property_id)
            .outerjoin(Agent, Agent.id == Review.agent_id)
            .where(Review.user_id == user.id)
            .order_by(Review.created_at.desc())
        )
    ).all()

    # If a moderator rejected a review or asked for edits, the author is
    # entitled to the reason they gave.
    notes = dict(
        (
            await session.execute(
                select(ModerationAction.review_id, ModerationAction.note)
                .where(
                    ModerationAction.review_id.in_([r.Review.id for r in rows] or [0]),
                    ModerationAction.note.is_not(None),
                )
                .order_by(ModerationAction.created_at)
            )
        ).all()
    )

    out = []
    for row in rows:
        item = _to_out(row.Review, row.property_id, row.company_name or row.name)
        item.moderator_note = notes.get(row.Review.id)
        # Only on your own reviews: the UI offers edit and delete exactly when
        # they will succeed, rather than promising a window and then 409-ing.
        remaining = review_service.edit_window_remaining(row.Review)
        item.edit_seconds_left = (
            0
            if row.Review.owner_response is not None
            else max(0, int(remaining.total_seconds()))
        )
        out.append(item)
    return out


@router.get("/properties/{property_id}/reviews", response_model=list[ReviewOut])
async def list_property_reviews(
    property_id: str,
    session: AsyncSession = Depends(get_session),
    viewer: User | None = Depends(get_optional_user),
    limit: int = 20,
    offset: int = 0,
) -> list[ReviewOut]:
    prop = (
        await session.execute(
            select(Property).where(Property.property_id == property_id.upper())
        )
    ).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail="property not found")

    # Only approved reviews are public. Un-moderated content carries real
    # defamation exposure (Section XV), so `pending` and `flagged` reviews stay
    # invisible until a human clears them — except to their own author, who
    # would otherwise think their submission vanished.
    visibility = Review.moderation_status == "approved"
    if viewer is not None:
        visibility = or_(
            visibility,
            and_(
                Review.user_id == viewer.id,
                Review.moderation_status != "rejected",
            ),
        )

    rows = (
        await session.execute(
            select(Review, Agent.company_name, Agent.name)
            .outerjoin(Agent, Agent.id == Review.agent_id)
            .where(Review.property_id == prop.id, visibility)
            .order_by(Review.created_at.desc())
            .limit(min(limit, 100))
            .offset(offset)
        )
    ).all()
    return [
        _to_out(row.Review, prop.property_id, row.company_name or row.name)
        for row in rows
    ]


async def _own_editable_review(
    session: AsyncSession, review_id: int, user: User
) -> Review:
    """Fetch a review the caller is allowed to amend, or raise.

    404 rather than 403 when it belongs to someone else: whether a given review
    id exists is not something a stranger needs confirmed.
    """
    review = (
        await session.execute(select(Review).where(Review.id == review_id))
    ).scalar_one_or_none()
    if review is None or review.user_id != user.id:
        raise HTTPException(status_code=404, detail="review not found")

    remaining = review_service.edit_window_remaining(review)
    if remaining.total_seconds() <= 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "The 48-hour window for changing this review has passed. "
                "Contact us if something in it is factually wrong."
            ),
        )
    if review.owner_response is not None:
        # A landlord has already replied. Rewriting the statement they answered
        # would leave their reply attached to words that were never said.
        raise HTTPException(
            status_code=409,
            detail=(
                "This review has a reply from the landlord or agent, so it can "
                "no longer be edited. Contact us if it is factually wrong."
            ),
        )
    return review


@router.patch("/reviews/{review_id}", response_model=ReviewSubmitResponse)
async def edit_my_review(
    review_id: int,
    payload: ReviewUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewSubmitResponse:
    """Amend your own review within 48 hours of posting.

    The Profile page has always promised this. Until now no endpoint existed,
    which meant a tenant who got a fact wrong about a named landlord — the exact
    thing the terms make them personally liable for — had no way to correct it.

    An edit returns the review to moderation, so this cannot be used to publish
    something bland and rewrite it once approved.
    """
    review = await _own_editable_review(session, review_id, user)
    updated, verdict = await review_service.update_review(session, review, payload)
    return ReviewSubmitResponse(
        review_id=updated.id,
        moderation_status=updated.moderation_status,
        flagged_reasons=verdict.reasons,
        message=(
            "Edit saved. It needs a quick manual check before it reappears."
            if verdict.needs_human
            else "Edit saved — back in the queue and republishing shortly."
        ),
    )


@router.delete("/reviews/{review_id}", status_code=204)
async def delete_my_review(
    review_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Withdraw your own review within 48 hours of posting."""
    review = await _own_editable_review(session, review_id, user)
    await review_service.delete_review(session, review, user)
    return Response(status_code=204)

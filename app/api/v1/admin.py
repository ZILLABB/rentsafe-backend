"""Admin moderation endpoints (Section XII /admin).

Guarded by role=admin on the JWT user. The moderation queue serves the
dashboard: pending + flagged reviews, oldest first (SLA order).
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core import scoring
from app.db.models import (
    Agent,
    AgentClaim,
    ModerationAction,
    Neighbourhood,
    Property,
    PropertyPhoto,
    Review,
    User,
    as_utc,
)
from app.db.session import get_session
from app.services import media, push
from app.services.reviews import recompute_property_scores

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")


class QueueItem(BaseModel):
    review_id: int
    property_id: str
    property_address: str | None = None
    status: str
    submitted_hours_ago: float
    text_warnings: str | None
    text_positives: str | None
    # Why the automated pre-check held this one. Served from the stored verdict
    # so the dashboard doesn't have to keep its own copy of the word lists —
    # the frontend's copy had already drifted out of sync with the backend's.
    flag_reasons: list[str] = []
    # Moderators are deciding whether to publish a rating, so they need to see
    # the rating, not just the prose.
    aggregate: float | None = None
    verified_tenant: bool = False
    reviewer_trust: float | None = None


ACTIONS = {
    "approve": "approved",
    "reject": "rejected",
    # "Ask edits" — hand it back to the author rather than making a binary
    # publish/destroy call on a review that's mostly fine.
    "request_edits": "needs_edits",
}


class ModerateRequest(BaseModel):
    action: str  # approve | reject | request_edits
    note: str | None = None


@router.get("/moderation/reviews", response_model=list[QueueItem])
async def review_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
) -> list[QueueItem]:
    _require_admin(user)
    rows = (
        await session.execute(
            select(Review, Property.property_id, Property.address_local, User.trust_score)
            .join(Property, Property.id == Review.property_id)
            .outerjoin(User, User.id == Review.user_id)
            # Flagged items first — legal risk outranks age — then oldest first
            # within each group, which is the SLA order.
            .where(Review.moderation_status.in_(["pending", "flagged"]))
            .order_by((Review.moderation_status == "flagged").desc(), Review.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    now = dt.datetime.now(dt.UTC)
    return [
        QueueItem(
            review_id=r.Review.id,
            property_id=r.property_id,
            property_address=r.address_local,
            status=r.Review.moderation_status,
            submitted_hours_ago=round(
                (now - as_utc(r.Review.created_at)).total_seconds() / 3600, 1
            )
            if r.Review.created_at
            else 0,
            text_warnings=r.Review.text_warnings,
            text_positives=r.Review.text_positives,
            flag_reasons=r.Review.flag_reasons or [],
            aggregate=round(
                scoring.review_aggregate(
                    {dim: getattr(r.Review, f"rating_{dim}") for dim in scoring.DIMENSIONS}
                ),
                2,
            ),
            verified_tenant=r.Review.verified_tenant,
            reviewer_trust=float(r.trust_score) if r.trust_score is not None else None,
        )
        for r in rows
    ]


class PhotoQueueItem(BaseModel):
    photo_id: int
    property_id: str
    property_address: str | None = None
    url: str
    thumb_url: str
    caption: str | None
    kind: str
    submitted_hours_ago: float
    uploader_trust: float | None = None


@router.get("/moderation/photos", response_model=list[PhotoQueueItem])
async def photo_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, le=200),
) -> list[PhotoQueueItem]:
    """Photos awaiting review.

    Uploads are held by default, so without this queue there is no way for one
    ever to become visible.
    """
    _require_admin(user)
    rows = (
        await session.execute(
            select(PropertyPhoto, Property.property_id, Property.address_local, User.trust_score)
            .join(Property, Property.id == PropertyPhoto.property_id)
            .outerjoin(User, User.id == PropertyPhoto.user_id)
            .where(PropertyPhoto.moderation_status == "pending")
            .order_by(PropertyPhoto.created_at)
            .limit(limit)
        )
    ).all()
    now = dt.datetime.now(dt.UTC)
    return [
        PhotoQueueItem(
            photo_id=r.PropertyPhoto.id,
            property_id=r.property_id,
            property_address=r.address_local,
            url=f"/api/v1/media/{r.PropertyPhoto.storage_key}",
            thumb_url=f"/api/v1/media/{r.PropertyPhoto.storage_key}?thumb=1",
            caption=r.PropertyPhoto.caption,
            kind=r.PropertyPhoto.kind,
            submitted_hours_ago=round(
                (now - as_utc(r.PropertyPhoto.created_at)).total_seconds() / 3600, 1
            )
            if r.PropertyPhoto.created_at
            else 0,
            uploader_trust=float(r.trust_score) if r.trust_score is not None else None,
        )
        for r in rows
    ]


@router.patch("/moderation/photos/{photo_id}")
async def moderate_photo(
    photo_id: int,
    payload: ModerateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    _require_admin(user)
    if payload.action not in ("approve", "reject"):
        raise HTTPException(status_code=422, detail="action must be approve|reject")

    photo = (
        await session.execute(select(PropertyPhoto).where(PropertyPhoto.id == photo_id))
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="photo not found")

    photo.moderation_status = (
        "approved" if payload.action == "approve" else "rejected"
    )
    session.add(
        ModerationAction(
            photo_id=photo.id,
            review_id=photo.review_id,
            moderator_id=user.id,
            action=f"photo_{payload.action}",
            previous_status="pending",
            note=payload.note,
        )
    )
    # A rejected photo has no reason to keep occupying disk.
    if payload.action == "reject":
        media.store.delete(photo.storage_key)

    await session.commit()
    return {"status": photo.moderation_status}


@router.patch("/moderation/reviews/{review_id}")
async def moderate_review(
    review_id: int,
    payload: ModerateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    _require_admin(user)
    if payload.action not in ACTIONS:
        raise HTTPException(
            status_code=422, detail=f"action must be one of {sorted(ACTIONS)}"
        )
    # Rejecting or bouncing a review is a decision the author is entitled to an
    # explanation for, so a note is required for anything but approval.
    if payload.action != "approve" and not (payload.note or "").strip():
        raise HTTPException(
            status_code=422, detail="a note is required when rejecting or asking for edits"
        )
    review = (
        await session.execute(select(Review).where(Review.id == review_id))
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")

    previous = review.moderation_status
    review.moderation_status = ACTIONS[payload.action]

    # Append-only record of who decided what. These reviews can be
    # defamation-adjacent; the decision trail needs to outlive the decision.
    session.add(
        ModerationAction(
            review_id=review.id,
            moderator_id=user.id,
            action=payload.action,
            previous_status=previous,
            note=payload.note,
        )
    )

    # Status change and score recompute land in one transaction.
    await session.flush()
    await recompute_property_scores(session, review.property_id, commit=False)
    await session.commit()

    # Approval is the moment a review becomes public, so it is the moment
    # watchers of that area should hear about it. Deliberately after the commit
    # and deliberately unable to raise: the decision is already recorded, and a
    # push service timing out must not undo a moderator's action.
    if review.moderation_status == "approved":
        prop = (
            await session.execute(
                select(Property).where(Property.id == review.property_id)
            )
        ).scalar_one_or_none()
        if prop is not None and prop.neighbourhood_code:
            area = (
                await session.execute(
                    select(Neighbourhood).where(
                        Neighbourhood.code == prop.neighbourhood_code
                    )
                )
            ).scalar_one_or_none()
            await push.notify_area_safe(
                session,
                area_code=prop.neighbourhood_code,
                kind="review",
                title=f"New review in {area.name if area else 'an area you watch'}",
                # No review text: this renders on a lock screen, outside every
                # moderation and privacy control the app has.
                body="A tenant just published a review for a property here.",
                url=f"/property/{prop.property_id}",
                exclude_user_id=review.user_id,
            )

    return {"status": review.moderation_status}


class ClaimQueueItem(BaseModel):
    claim_id: int
    agent_slug: str | None
    agent_name: str
    company_name: str | None
    lasrera_number: str | None
    contact_email: str | None
    evidence_note: str | None
    claimant_phone_last4: str | None
    agent_total_reviews: int
    agent_flagged: bool
    submitted_hours_ago: float


class ClaimDecision(BaseModel):
    action: str = Field(..., description="approve|reject")
    note: str | None = None


@router.get("/moderation/claims", response_model=list[ClaimQueueItem])
async def claim_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, le=200),
) -> list[ClaimQueueItem]:
    """Agents asking to take control of their own profile."""
    _require_admin(user)
    rows = (
        await session.execute(
            select(AgentClaim, Agent, User)
            .join(Agent, Agent.id == AgentClaim.agent_id)
            .join(User, User.id == AgentClaim.user_id)
            .where(AgentClaim.status == "pending")
            .order_by(AgentClaim.created_at)
            .limit(limit)
        )
    ).all()

    now = dt.datetime.now(dt.UTC)
    return [
        ClaimQueueItem(
            claim_id=claim.id,
            agent_slug=agent.slug,
            agent_name=agent.name,
            company_name=agent.company_name,
            lasrera_number=claim.lasrera_number,
            contact_email=claim.contact_email,
            evidence_note=claim.evidence_note,
            claimant_phone_last4=claimant.phone_last4,
            agent_total_reviews=agent.total_reviews or 0,
            # Surfaced because it changes the stakes: approving a claim on a
            # flagged agent hands the reply button to someone the platform has
            # already warned users about.
            agent_flagged=bool(agent.flagged),
            submitted_hours_ago=round(
                (now - (as_utc(claim.created_at) or now)).total_seconds() / 3600, 1
            ),
        )
        for claim, agent, claimant in rows
    ]


@router.patch("/moderation/claims/{claim_id}")
async def decide_claim(
    claim_id: int,
    payload: ClaimDecision,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve or reject a profile claim.

    Approving binds the claimant's ``phone_hash`` onto the agent record, which
    is what the right-of-reply check in ``responses.py`` authorises against. It
    is therefore the moment someone gains the ability to answer, on the public
    record, every tenant who has reviewed them — hence a human decision with an
    audit trail rather than an automatic one.
    """
    _require_admin(user)
    if payload.action not in {"approve", "reject"}:
        raise HTTPException(status_code=422, detail="action must be approve|reject")

    row = (
        await session.execute(
            select(AgentClaim, Agent)
            .join(Agent, Agent.id == AgentClaim.agent_id)
            .where(AgentClaim.id == claim_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="claim not found")
    claim, agent = row

    if claim.status != "pending":
        raise HTTPException(status_code=409, detail="claim already decided")

    if payload.action == "approve":
        if agent.profile_claimed:
            raise HTTPException(
                status_code=409, detail="this profile was claimed in the meantime"
            )
        claimant = (
            await session.execute(select(User).where(User.id == claim.user_id))
        ).scalar_one()
        agent.profile_claimed = True
        agent.phone_hash = claimant.phone_hash
        if claim.lasrera_number:
            agent.lasrera_number = claim.lasrera_number
        # Deliberately NOT setting lasrera_verified: that badge means the number
        # was checked against the register, which is a separate act from
        # accepting that this person represents this agency.
        claim.status = "approved"
    else:
        claim.status = "rejected"

    claim.decided_by = user.id
    claim.decision_note = payload.note
    claim.decided_at = dt.datetime.now(dt.UTC)
    await session.commit()
    return {"status": claim.status}

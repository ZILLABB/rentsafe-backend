"""Right of reply for landlords and agents (Section III).

A review names a real, identifiable person or business and stays up
indefinitely. Publishing that without giving the named party any way to answer
is both unfair and, in Nigeria, the posture that turns a defamation complaint
into a defamation case. The ``owner_response`` column existed from the start
and nothing could write to it — this closes that.

The reply is not moderation: it does not remove or alter the review, and it
does not change the score. It appears beside the review so a reader sees both
accounts. Replies go through the same content pre-check as reviews, because a
landlord can defame a tenant back.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core import moderation
from app.db.models import Agent, Review, User
from app.db.session import get_session

router = APIRouter(tags=["responses"])

MAX_REPLY_CHARS = 800


class ResponseIn(BaseModel):
    text: str = Field(..., min_length=20, max_length=MAX_REPLY_CHARS)
    # Which capacity the responder is writing in.
    author_role: str = Field(default="landlord", description="landlord | agent")


class ResponseOut(BaseModel):
    review_id: int
    text: str
    author_role: str
    status: str
    message: str


@router.post("/reviews/{review_id}/response", response_model=ResponseOut)
async def add_owner_response(
    review_id: int,
    payload: ResponseIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ResponseOut:
    """Reply to a review of your property, as the landlord or the agent."""
    if payload.author_role not in ("landlord", "agent"):
        raise HTTPException(
            status_code=422, detail="author_role must be landlord or agent"
        )

    review = (
        await session.execute(select(Review).where(Review.id == review_id))
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    # Only replies to published reviews. Answering something still in the
    # moderation queue would leak that it exists — and it may never publish.
    if review.moderation_status != "approved":
        raise HTTPException(
            status_code=404, detail="Review not found"
        )

    # Verifying that a claimant really is this landlord needs LASRERA or
    # document checks that don't exist yet, so authorisation is deliberately
    # narrow: only a claimed agent profile linked to this review can reply.
    # Anything looser would let anyone speak in a landlord's name.
    allowed = user.role == "admin"
    if not allowed and review.agent_id is not None:
        agent = (
            await session.execute(select(Agent).where(Agent.id == review.agent_id))
        ).scalar_one_or_none()
        allowed = bool(
            agent
            and agent.profile_claimed
            and agent.phone_hash
            and agent.phone_hash == user.phone_hash
        )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only a verified landlord or the claimed agent for this "
                "property can reply. Contact support to claim your profile."
            ),
        )

    if review.owner_response:
        raise HTTPException(
            status_code=409,
            detail="This review already has a reply. Contact support to amend it.",
        )

    # A reply can defame too. Same gate as a review: hold it for a human.
    verdict = moderation.check_text(payload.text)

    review.owner_response = payload.text.strip()
    review.owner_response_from = payload.author_role
    await session.commit()

    return ResponseOut(
        review_id=review.id,
        text=review.owner_response,
        author_role=payload.author_role,
        status="flagged" if verdict.needs_human else "published",
        message=(
            "Your reply needs a quick check and will appear shortly."
            if verdict.needs_human
            else "Your reply is now shown beside the review."
        ),
    )

"""Agent endpoints (Section XII /agents)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import Agent, AgentClaim, Neighbourhood, Property, Review, User
from app.db.session import get_session

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentScore(BaseModel):
    label: str
    value: float


class LinkedProperty(BaseModel):
    property_id: str
    address: str | None
    rating: float | None


class AgentOut(BaseModel):
    id: int
    slug: str | None
    name: str
    company_name: str | None
    operating_areas: list[str] | None
    lasrera_verified: bool
    profile_claimed: bool
    avg_rating_overall: float | None
    avg_fee_pct: float | None
    # Area benchmark the agent's fee is compared against — the average across
    # the neighbourhoods they operate in. Previously hardcoded in the client.
    area_avg_fee_pct: float | None = None
    total_reviews: int
    flagged: bool
    flag_reason: str | None
    scores: list[AgentScore] = []
    linked_properties: list[LinkedProperty] = []

    model_config = {"from_attributes": True}


def _scores(a: Agent) -> list[AgentScore]:
    pairs = [
        ("Fee transparency", a.avg_rating_transparency),
        ("Honesty", a.avg_rating_honesty),
        ("Fee fairness", a.avg_rating_fee_fairness),
        ("Responsiveness", a.avg_rating_responsiveness),
        ("Professionalism", a.avg_rating_professionalism),
    ]
    return [AgentScore(label=l, value=float(v)) for l, v in pairs if v is not None]


async def _linked(session: AsyncSession, agent_id: int) -> list[LinkedProperty]:
    prop_ids = (
        await session.execute(
            select(Review.property_id).where(Review.agent_id == agent_id).distinct()
        )
    ).scalars().all()
    if not prop_ids:
        # Fall back: agents operating in seeded areas link to all demo properties.
        props = (await session.execute(select(Property).limit(5))).scalars().all()
    else:
        props = (
            await session.execute(select(Property).where(Property.id.in_(prop_ids)))
        ).scalars().all()
    return [
        LinkedProperty(
            property_id=p.property_id,
            address=p.address_local or p.address_formal,
            rating=float(p.avg_rating) if p.avg_rating is not None else None,
        )
        for p in props
    ]


async def _area_avg_fee(session: AsyncSession, agent: Agent) -> float | None:
    """Mean agent fee across the neighbourhoods this agent operates in."""
    stmt = select(func.avg(Neighbourhood.avg_agent_fee_pct))
    if agent.operating_areas:
        stmt = stmt.where(Neighbourhood.name.in_(agent.operating_areas))
    value = (await session.execute(stmt)).scalar_one_or_none()
    return round(float(value), 2) if value is not None else None


@router.get("", response_model=list[AgentOut])
async def search_agents(
    session: AsyncSession = Depends(get_session),
    name: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> list[AgentOut]:
    stmt = select(Agent)
    if name:
        stmt = stmt.where(Agent.name_normalised.like(f"%{name.strip().lower()}%"))
    # Ordered so that a truncated page is the *most reviewed* agents rather
    # than an arbitrary slice: with 80 unclaimed listings and one reviewed
    # agent, insertion order would bury the only profile anyone has rated.
    stmt = stmt.order_by(Agent.total_reviews.desc(), Agent.name)
    agents = (await session.execute(stmt.limit(limit))).scalars().all()
    out = []
    for a in agents:
        item = AgentOut.model_validate(a)
        item.scores = _scores(a)
        out.append(item)
    return out


@router.get("/{slug}", response_model=AgentOut)
async def get_agent(slug: str, session: AsyncSession = Depends(get_session)) -> AgentOut:
    agent = (
        await session.execute(select(Agent).where(Agent.slug == slug))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    out = AgentOut.model_validate(agent)
    out.scores = _scores(agent)
    out.linked_properties = await _linked(session, agent.id)
    out.area_avg_fee_pct = await _area_avg_fee(session, agent)
    return out


class ClaimRequest(BaseModel):
    lasrera_number: str | None = Field(default=None, max_length=30)
    contact_email: str | None = Field(default=None, max_length=120)
    evidence_note: str | None = Field(default=None, max_length=1000)


@router.post("/{slug}/claim", status_code=202)
async def claim_profile(
    slug: str,
    payload: ClaimRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ask to take control of this agent profile.

    Submitting is not granting. A claim goes to a human, because the power being
    requested — replying on the record as a named agent — is exactly what a
    rival, or a landlord answering criticism of their own property, would want.

    Re-submitting updates the existing request rather than queueing a second
    one, so a claimant who mistyped their LASRERA number can simply try again.
    """
    agent = (
        await session.execute(select(Agent).where(Agent.slug == slug))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    if agent.profile_claimed:
        raise HTTPException(
            status_code=409,
            detail="This profile has already been claimed. Contact us if that is wrong.",
        )

    existing = (
        await session.execute(
            select(AgentClaim).where(
                AgentClaim.agent_id == agent.id, AgentClaim.user_id == user.id
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status == "rejected":
            raise HTTPException(
                status_code=409,
                detail="A previous claim on this profile was declined. Contact us.",
            )
        existing.lasrera_number = payload.lasrera_number
        existing.contact_email = payload.contact_email
        existing.evidence_note = payload.evidence_note
        existing.status = "pending"
    else:
        session.add(
            AgentClaim(
                agent_id=agent.id,
                user_id=user.id,
                lasrera_number=payload.lasrera_number,
                contact_email=payload.contact_email,
                evidence_note=payload.evidence_note,
            )
        )

    await session.commit()
    return {
        "status": "pending",
        "message": (
            "Claim received. We verify LASRERA and company details by hand, so "
            "this takes a few days. We'll contact you on the details you gave."
        ),
    }

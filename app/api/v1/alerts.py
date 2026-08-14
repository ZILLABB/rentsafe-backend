"""Activity feed and area watches (Section XII /alerts).

Derived from what has actually happened in the database — newly approved
reviews, newly reported flooding, agents that have crossed the blacklist
threshold.

A signed-in user watching areas gets those areas by default; everyone else
gets all of Lagos. There is still no push/SMS delivery, so this is an in-app
feed rather than notifications, and the UI says so rather than implying a
subscription that would text you.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user
from app.db.models import (
    Agent,
    AreaWatch,
    FloodEvent,
    Neighbourhood,
    Property,
    Review,
    User,
    as_utc,
)
from app.db.session import get_session

router = APIRouter(prefix="/alerts", tags=["alerts"])
watch_router = APIRouter(tags=["alerts"])


class AlertOut(BaseModel):
    kind: str                      # review | flood | agent_flag
    tone: str                      # info | mid | bad
    title: str
    detail: str | None = None
    property_id: str | None = None
    agent_slug: str | None = None
    area_code: str | None = None
    area_name: str | None = None
    hours_ago: float | None = None
    # True when this happened since the viewer last opened their alerts.
    unread: bool = False


class WatchOut(BaseModel):
    area_code: str
    area_name: str
    notify_reviews: bool
    notify_floods: bool
    notify_agent_flags: bool
    # Activity in this area since the user last read their alerts.
    unread_count: int = 0


class WatchRequest(BaseModel):
    notify_reviews: bool = True
    notify_floods: bool = True
    notify_agent_flags: bool = True


class UnreadOut(BaseModel):
    unread: int
    watching: int


def _hours_since(value: dt.datetime | None, now: dt.datetime) -> float | None:
    aware = as_utc(value)
    if aware is None:
        return None
    return round((now - aware).total_seconds() / 3600, 1)


async def _watched_codes(session: AsyncSession, user: User | None) -> list[str]:
    if user is None:
        return []
    return list(
        (
            await session.execute(
                select(AreaWatch.area_code).where(AreaWatch.user_id == user.id)
            )
        ).scalars().all()
    )


async def _build_feed(
    session: AsyncSession,
    *,
    areas: list[str] | None,
    limit: int,
    read_at: dt.datetime | None,
) -> list[AlertOut]:
    """Assemble the feed. ``areas=None`` means all of Lagos."""
    now = dt.datetime.now(dt.UTC)
    reviews_out: list[AlertOut] = []
    floods_out: list[AlertOut] = []
    agents_out: list[AlertOut] = []

    # Reviews are by far the most numerous event, so each kind gets its own
    # share of the page. Otherwise a busy week of reviews buries every flood
    # report — which is the one thing on here somebody might act on today.
    per_kind = max(3, limit // 2)

    def is_unread(when: dt.datetime | None) -> bool:
        aware = as_utc(when)
        if aware is None or read_at is None:
            return read_at is None and aware is not None
        return aware > read_at

    # Only approved reviews — the same rule the public review list follows, so
    # this can't become a side channel for content moderation withheld.
    review_stmt = (
        select(Review, Property, Neighbourhood.code, Neighbourhood.name)
        .join(Property, Property.id == Review.property_id)
        .outerjoin(Neighbourhood, Neighbourhood.code == Property.neighbourhood_code)
        .where(Review.moderation_status == "approved")
        .order_by(Review.created_at.desc())
        .limit(per_kind)
    )
    if areas is not None:
        review_stmt = review_stmt.where(Property.neighbourhood_code.in_(areas))

    for review, prop, area_code, area_name in (await session.execute(review_stmt)).all():
        reviews_out.append(
            AlertOut(
                kind="review",
                tone="info",
                title=f"New review in {area_name or prop.lga_code}",
                detail=prop.address_local or prop.property_id,
                property_id=prop.property_id,
                area_code=area_code,
                area_name=area_name,
                hours_ago=_hours_since(review.created_at, now),
                unread=is_unread(review.created_at),
            )
        )

    flood_stmt = (
        select(FloodEvent, Property, Neighbourhood.code, Neighbourhood.name)
        .join(Property, Property.id == FloodEvent.property_id)
        .outerjoin(Neighbourhood, Neighbourhood.code == Property.neighbourhood_code)
        .order_by(FloodEvent.id.desc())
        .limit(per_kind)
    )
    if areas is not None:
        flood_stmt = flood_stmt.where(Property.neighbourhood_code.in_(areas))

    for event, prop, area_code, area_name in (await session.execute(flood_stmt)).all():
        floods_out.append(
            AlertOut(
                kind="flood",
                tone="bad" if event.severity == "major" else "mid",
                title=f"Flood report — {area_name or prop.lga_code}",
                detail=f"{event.when_label}: {event.quote}",
                property_id=prop.property_id,
                area_code=area_code,
                area_name=area_name,
            )
        )

    # Agent flags aren't area-scoped in the data model, so they only appear in
    # the all-Lagos view rather than being attached to an arbitrary area.
    if areas is None:
        agents = (
            await session.execute(
                select(Agent).where(Agent.flagged.is_(True)).limit(per_kind)
            )
        ).scalars().all()
        for agent in agents:
            agents_out.append(
                AlertOut(
                    kind="agent_flag",
                    tone="mid",
                    title=f"Agent flagged: {agent.company_name or agent.name}",
                    detail=agent.flag_reason,
                    agent_slug=agent.slug,
                )
            )

    out = reviews_out + floods_out + agents_out
    # Newest first within the dated items; floods carry a "OCT 2024"-style
    # label rather than a timestamp, so they sort after the dated ones.
    out.sort(key=lambda a: (a.hours_ago is None, a.hours_ago or 0))
    return out[:limit]


@router.get("", response_model=list[AlertOut])
async def recent_activity(
    session: AsyncSession = Depends(get_session),
    viewer: User | None = Depends(get_optional_user),
    scope: str = Query(
        default="auto",
        description="auto | watched | all — 'auto' uses your watches when you have any",
    ),
    area: str | None = Query(default=None, description="Filter to one area code"),
    limit: int = Query(default=20, le=50),
) -> list[AlertOut]:
    watched = await _watched_codes(session, viewer)

    if area:
        areas: list[str] | None = [area.upper()]
    elif scope == "all":
        areas = None
    elif scope == "watched":
        # An explicit request for watched areas with none set returns nothing,
        # which is honest — the alternative silently shows all of Lagos and
        # looks like the watches aren't working.
        areas = watched
    else:
        areas = watched or None

    return await _build_feed(
        session,
        areas=areas,
        limit=limit,
        read_at=as_utc(viewer.alerts_read_at) if viewer else None,
    )


@router.get("/unread", response_model=UnreadOut)
async def unread_count(
    session: AsyncSession = Depends(get_session),
    viewer: User | None = Depends(get_optional_user),
) -> UnreadOut:
    """How much has happened in your watched areas since you last looked.

    Drives the nav badge, which used to be permanently lit.
    """
    if viewer is None:
        return UnreadOut(unread=0, watching=0)

    watched = await _watched_codes(session, viewer)
    if not watched:
        return UnreadOut(unread=0, watching=0)

    read_at = as_utc(viewer.alerts_read_at)
    stmt = (
        select(func.count())
        .select_from(Review)
        .join(Property, Property.id == Review.property_id)
        .where(
            Review.moderation_status == "approved",
            Property.neighbourhood_code.in_(watched),
        )
    )
    if read_at is not None:
        stmt = stmt.where(Review.created_at > read_at)

    return UnreadOut(
        unread=(await session.execute(stmt)).scalar_one(), watching=len(watched)
    )


@router.post("/read", status_code=204)
async def mark_read(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Mark the feed as seen, clearing the unread badge."""
    user.alerts_read_at = dt.datetime.now(dt.UTC)
    await session.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------- watches

@watch_router.get("/users/me/watches", response_model=list[WatchOut])
async def list_watches(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[WatchOut]:
    rows = (
        await session.execute(
            select(AreaWatch, Neighbourhood.name)
            .join(Neighbourhood, Neighbourhood.code == AreaWatch.area_code)
            .where(AreaWatch.user_id == user.id)
            .order_by(Neighbourhood.name)
        )
    ).all()

    read_at = as_utc(user.alerts_read_at)
    out = []
    for watch, area_name in rows:
        stmt = (
            select(func.count())
            .select_from(Review)
            .join(Property, Property.id == Review.property_id)
            .where(
                Review.moderation_status == "approved",
                Property.neighbourhood_code == watch.area_code,
            )
        )
        if read_at is not None:
            stmt = stmt.where(Review.created_at > read_at)
        out.append(
            WatchOut(
                area_code=watch.area_code,
                area_name=area_name,
                notify_reviews=watch.notify_reviews,
                notify_floods=watch.notify_floods,
                notify_agent_flags=watch.notify_agent_flags,
                unread_count=(await session.execute(stmt)).scalar_one(),
            )
        )
    return out


@watch_router.put("/areas/{area_code}/watch", status_code=204)
async def watch_area(
    area_code: str,
    payload: WatchRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Start watching an area. Idempotent; updates preferences if already set."""
    code = area_code.upper()
    exists = (
        await session.execute(
            select(func.count())
            .select_from(Neighbourhood)
            .where(Neighbourhood.code == code)
        )
    ).scalar_one()
    if not exists:
        raise HTTPException(status_code=404, detail="Unknown area")

    watch = (
        await session.execute(
            select(AreaWatch).where(
                AreaWatch.user_id == user.id, AreaWatch.area_code == code
            )
        )
    ).scalar_one_or_none()

    prefs = payload or WatchRequest()
    if watch is None:
        session.add(
            AreaWatch(
                user_id=user.id,
                area_code=code,
                notify_reviews=prefs.notify_reviews,
                notify_floods=prefs.notify_floods,
                notify_agent_flags=prefs.notify_agent_flags,
            )
        )
    else:
        watch.notify_reviews = prefs.notify_reviews
        watch.notify_floods = prefs.notify_floods
        watch.notify_agent_flags = prefs.notify_agent_flags

    await session.commit()
    return Response(status_code=204)


@watch_router.delete("/areas/{area_code}/watch", status_code=204)
async def unwatch_area(
    area_code: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await session.execute(
        delete(AreaWatch).where(
            AreaWatch.user_id == user.id, AreaWatch.area_code == area_code.upper()
        )
    )
    await session.commit()
    return Response(status_code=204)

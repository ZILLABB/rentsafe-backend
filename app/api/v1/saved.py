"""Saved properties (bookmarks).

The Profile page counted these before anything stored them.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core import property_id as pid_core
from app.db.models import Neighbourhood, Property, SavedProperty, User, as_utc
from app.db.session import get_session

router = APIRouter(tags=["saved"])


class SavedOut(BaseModel):
    property_id: str
    address: str | None
    area_name: str | None
    avg_rating: float | None
    total_reviews: int
    flood_zone: str | None
    latest_rent_kobo: int | None
    note: str | None
    saved_at: dt.datetime


class SaveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


async def _get_property(session: AsyncSession, property_id: str) -> Property:
    try:
        pid_core.parse(property_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    prop = (
        await session.execute(
            select(Property).where(Property.property_id == property_id.upper())
        )
    ).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop


@router.get("/users/me/saved", response_model=list[SavedOut])
async def list_saved(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SavedOut]:
    rows = (
        await session.execute(
            select(SavedProperty, Property, Neighbourhood.name)
            .join(Property, Property.id == SavedProperty.property_id)
            .outerjoin(
                Neighbourhood, Neighbourhood.code == Property.neighbourhood_code
            )
            .where(SavedProperty.user_id == user.id)
            .order_by(SavedProperty.created_at.desc())
        )
    ).all()
    return [
        SavedOut(
            property_id=prop.property_id,
            address=prop.address_local or prop.address_formal,
            area_name=area_name,
            avg_rating=float(prop.avg_rating) if prop.avg_rating is not None else None,
            total_reviews=prop.total_reviews,
            flood_zone=prop.flood_zone,
            latest_rent_kobo=prop.latest_rent_kobo,
            note=saved.note,
            saved_at=as_utc(saved.created_at) or dt.datetime.now(dt.UTC),
        )
        for saved, prop, area_name in rows
    ]


@router.put("/properties/{property_id}/save", status_code=204)
async def save_property(
    property_id: str,
    payload: SaveRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Bookmark a property. Idempotent — saving twice is not an error."""
    prop = await _get_property(session, property_id)

    existing = (
        await session.execute(
            select(SavedProperty).where(
                SavedProperty.user_id == user.id,
                SavedProperty.property_id == prop.id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            SavedProperty(
                user_id=user.id,
                property_id=prop.id,
                note=(payload.note if payload else None),
            )
        )
    elif payload and payload.note is not None:
        existing.note = payload.note

    await session.commit()
    return Response(status_code=204)


@router.delete("/properties/{property_id}/save", status_code=204)
async def unsave_property(
    property_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Remove a bookmark. Idempotent."""
    prop = await _get_property(session, property_id)
    await session.execute(
        delete(SavedProperty).where(
            SavedProperty.user_id == user.id,
            SavedProperty.property_id == prop.id,
        )
    )
    await session.commit()
    return Response(status_code=204)

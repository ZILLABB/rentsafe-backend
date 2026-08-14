"""Photo upload and serving (Section II step 3, Section III evidence).

Uploads are stripped of metadata, re-encoded and held for moderation before
anyone else can see them — the same rule review text follows, for the same
reason: this is user-generated content about a real, identifiable address.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, get_optional_user
from app.core import phash as phash_core
from app.core import property_id as pid_core
from app.db.models import Property, PropertyPhoto, User, as_utc
from app.db.session import get_session
from app.services import media

# How many uploads may be decoding at once, per process.
#
# The alternative considered was a Celery worker — the dependency was declared
# and never imported. It was dropped rather than wired: the client waits for the
# photo record in the response, so deferring the work to a queue would not make
# the user's upload faster, it would only add a second deployable to run and
# monitor. What actually needed fixing was the unbounded fan-out into the
# thread pool, which is this.
#
# Sized below Starlette's default 40-thread pool so image work can never take
# every thread and starve the blocking calls other endpoints make.
UPLOAD_CONCURRENCY = 8
_upload_slots = asyncio.Semaphore(UPLOAD_CONCURRENCY)

router = APIRouter(tags=["photos"])


class PhotoOut(BaseModel):
    id: int
    url: str
    thumb_url: str
    width: int
    height: int
    caption: str | None
    kind: str
    moderation_status: str
    created_at: dt.datetime


class UploadResult(BaseModel):
    photo: PhotoOut
    message: str
    # True when this image perceptually matches one already on the property —
    # useful signal for a moderator, and for the identity system.
    duplicate_of_photo_id: int | None = None


def _to_out(p: PropertyPhoto) -> PhotoOut:
    return PhotoOut(
        id=p.id,
        url=f"/api/v1/media/{p.storage_key}",
        thumb_url=f"/api/v1/media/{p.storage_key}?thumb=1",
        width=p.width,
        height=p.height,
        caption=p.caption,
        kind=p.kind,
        moderation_status=p.moderation_status,
        created_at=as_utc(p.created_at) or dt.datetime.now(dt.UTC),
    )


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


@router.get("/properties/{property_id}/photos", response_model=list[PhotoOut])
async def list_photos(
    property_id: str,
    session: AsyncSession = Depends(get_session),
    viewer: User | None = Depends(get_optional_user),
) -> list[PhotoOut]:
    """Approved photos, plus the viewer's own pending ones."""
    prop = await _get_property(session, property_id)

    visibility = PropertyPhoto.moderation_status == "approved"
    if viewer is not None:
        visibility = or_(
            visibility,
            (PropertyPhoto.user_id == viewer.id)
            & (PropertyPhoto.moderation_status != "rejected"),
        )

    rows = (
        await session.execute(
            select(PropertyPhoto)
            .where(PropertyPhoto.property_id == prop.id, visibility)
            .order_by(PropertyPhoto.created_at.desc())
            .limit(24)
        )
    ).scalars().all()
    return [_to_out(p) for p in rows]


@router.post(
    "/properties/{property_id}/photos", response_model=UploadResult, status_code=201
)
async def upload_photo(
    property_id: str,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    kind: str = Form(default="property"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UploadResult:
    prop = await _get_property(session, property_id)

    raw = await file.read()
    # One slot per concurrent upload. Without a bound, a burst of uploads takes
    # every thread in the default pool, and unrelated requests that need a
    # thread — any blocking call anywhere — queue behind image decoding.
    async with _upload_slots:
        try:
            # Decode, EXIF strip, resize and hash is ~285ms of CPU for a 12MP
            # phone photo. Run inline it would hold the event loop for that
            # whole time and stall every other request; Pillow releases the GIL
            # for most of it, so a worker thread genuinely parallelises.
            processed = await run_in_threadpool(
                media.process_upload, raw, filename=file.filename
            )
        except media.ImageRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Does this look like a photo already on file? Cheap enough to scan: a
    # property has tens of photos, not thousands.
    duplicate_id: int | None = None
    existing = (
        await session.execute(
            select(PropertyPhoto).where(
                PropertyPhoto.property_id == prop.id,
                PropertyPhoto.phash.is_not(None),
                PropertyPhoto.moderation_status != "rejected",
            )
        )
    ).scalars().all()
    for other in existing:
        if other.phash and phash_core.is_same_building(processed.phash, other.phash):
            duplicate_id = other.id
            break

    # Also off the loop: with object storage configured this is a synchronous
    # network round trip to S3, and boto3 has no async client. On local disk it
    # is a fast write, which is exactly why the blocking version survived
    # review — the cost only appears once media moves off the container.
    await run_in_threadpool(media.store.save, processed)

    photo = PropertyPhoto(
        property_id=prop.id,
        user_id=user.id,
        storage_key=processed.storage_key,
        width=processed.width,
        height=processed.height,
        phash=processed.phash,
        caption=(caption or "").strip() or None,
        kind="evidence" if kind == "evidence" else "property",
    )
    session.add(photo)

    # First photo of a building doubles as the identity signal for pHash
    # matching during registration (Section II step 3).
    if prop.photo_hash is None:
        prop.photo_hash = processed.phash

    await session.commit()
    await session.refresh(photo)

    return UploadResult(
        photo=_to_out(photo),
        message=(
            "Photo received. Metadata was stripped and it will appear once "
            "a moderator checks it."
        ),
        duplicate_of_photo_id=duplicate_id,
    )


@router.delete("/photos/{photo_id}", status_code=204)
async def delete_photo(
    photo_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Withdraw your own photo."""
    photo = (
        await session.execute(select(PropertyPhoto).where(PropertyPhoto.id == photo_id))
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    if photo.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="That isn't your photo")

    media.store.delete(photo.storage_key)
    await session.delete(photo)
    await session.commit()
    return Response(status_code=204)


@router.get("/media/{key}")
async def serve_media(
    key: str,
    thumb: int = 0,
    session: AsyncSession = Depends(get_session),
    viewer: User | None = Depends(get_optional_user),
) -> Response:
    """Serve a stored image.

    Access follows the same rule as the listing: approved images are public,
    an author can see their own while it's pending. Serving bytes without that
    check would make the moderation gate decorative — the URL is guessable
    enough to matter.
    """
    photo = (
        await session.execute(
            select(PropertyPhoto).where(PropertyPhoto.storage_key == key)
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Not found")

    is_owner = viewer is not None and viewer.id == photo.user_id
    is_admin = viewer is not None and viewer.role == "admin"
    if photo.moderation_status != "approved" and not (is_owner or is_admin):
        raise HTTPException(status_code=404, detail="Not found")

    data = media.store.load(key, thumb=bool(thumb))
    if data is None:
        raise HTTPException(status_code=404, detail="Not found")

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            # Everything stored is a re-encoded JPEG, but say so explicitly and
            # forbid content-type sniffing so a browser can never be talked
            # into treating an image response as script.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=604800"
            if photo.moderation_status == "approved"
            else "private, no-store",
        },
    )

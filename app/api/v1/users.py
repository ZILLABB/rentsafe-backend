"""Current-user endpoint (Section XII /users)."""

from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.ratelimit import client_ip as resolve_client_ip
from app.core import scoring, security
from app.db.models import (
    AreaWatch,
    CommuteReport,
    Property,
    PropertyPhoto,
    Review,
    SavedProperty,
    User,
    as_utc,
)
from app.db.session import get_session
from app.services import auth as auth_service
from app.services import media, otp_store, sms
from app.services.otp_store import OTPThrottled

router = APIRouter(prefix="/users", tags=["users"])


class MeOut(BaseModel):
    id: int
    display_name: str | None
    phone_last4: str | None
    role: str
    review_count: int
    trust_score: float
    nin_verified: bool
    is_anonymous_default: bool

    model_config = {"from_attributes": True}


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    """The signed-in user's own profile.

    The Profile page previously displayed a hardcoded trust score and initial.
    """
    return MeOut.model_validate(user)


@router.get("/me/export")
async def export_my_data(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Everything we hold about this user, as JSON.

    NDPR gives data subjects a right of access. It is also the honest thing for
    a transparency product to offer: we ask people to publish information about
    their homes, so they should be able to see exactly what that amounts to.
    """
    reviews = (
        await session.execute(
            select(Review, Property.property_id)
            .join(Property, Property.id == Review.property_id)
            .where(Review.user_id == user.id)
        )
    ).all()
    photos = (
        await session.execute(
            select(PropertyPhoto).where(PropertyPhoto.user_id == user.id)
        )
    ).scalars().all()
    saved = (
        await session.execute(
            select(Property.property_id)
            .join(SavedProperty, SavedProperty.property_id == Property.id)
            .where(SavedProperty.user_id == user.id)
        )
    ).scalars().all()
    watches = (
        await session.execute(
            select(AreaWatch.area_code).where(AreaWatch.user_id == user.id)
        )
    ).scalars().all()
    commutes = (
        await session.execute(
            select(CommuteReport).where(CommuteReport.user_id == user.id)
        )
    ).scalars().all()

    return {
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "account": {
            # The phone number itself is not stored — only a peppered hash,
            # which is not reversible and so is not returned.
            "display_name": user.display_name,
            "phone_last4": user.phone_last4,
            "role": user.role,
            "trust_score": float(user.trust_score),
            "created_at": as_utc(user.created_at).isoformat()
            if user.created_at
            else None,
        },
        "reviews": [
            {
                "property_id": pid,
                "tenancy_start": r.tenancy_start.isoformat(),
                "tenancy_end": r.tenancy_end.isoformat() if r.tenancy_end else None,
                "rent_amount_kobo": r.rent_amount_kobo,
                "ratings": {
                    d: getattr(r, f"rating_{d}") for d in scoring.DIMENSIONS
                },
                "text_positives": r.text_positives,
                "text_warnings": r.text_warnings,
                "moderation_status": r.moderation_status,
                "created_at": as_utc(r.created_at).isoformat()
                if r.created_at
                else None,
            }
            for r, pid in reviews
        ],
        "photos": [
            {"id": p.id, "caption": p.caption, "status": p.moderation_status}
            for p in photos
        ],
        "saved_properties": list(saved),
        "watched_areas": list(watches),
        "commute_reports": [
            {
                "destination": c.destination_code,
                "window": c.departure_window,
                "mode": c.mode,
                "minutes": c.minutes,
            }
            for c in commutes
        ],
    }


class DeleteAccountRequest(BaseModel):
    # Typing the word is a deliberate speed bump on an irreversible action.
    confirm: str = Field(..., description='Must be the word "DELETE"')
    # Reviews are the public record other tenants rely on. Deleting an account
    # anonymises them by default rather than erasing them; the user can ask for
    # full removal instead.
    remove_reviews: bool = False


@router.post("/me/delete", status_code=200)
async def delete_my_account(
    payload: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Close this account (NDPR right to erasure).

    The default anonymises rather than erases contributions. That is a real
    tension: erasure is a data-subject right, but a review platform whose
    record can be deleted on demand is one a landlord can pressure a tenant
    into emptying. Anonymising keeps the public record while removing the link
    to a person; full removal stays available on request.
    """
    if payload.confirm != "DELETE":
        raise HTTPException(status_code=422, detail='Send confirm: "DELETE" to proceed.')

    reviews = (
        await session.execute(select(Review).where(Review.user_id == user.id))
    ).scalars().all()

    if payload.remove_reviews:
        for r in reviews:
            r.moderation_status = "rejected"  # withdrawn: no longer public
        removed = len(reviews)
        anonymised = 0
    else:
        for r in reviews:
            r.is_anonymous = True
            r.display_name = "Former tenant"
        removed = 0
        anonymised = len(reviews)

    # Photos are tied to an identifiable place and were uploaded by this
    # person, so they go regardless.
    photos = (
        await session.execute(
            select(PropertyPhoto).where(PropertyPhoto.user_id == user.id)
        )
    ).scalars().all()
    for p in photos:
        media.store.delete(p.storage_key)
        await session.delete(p)

    await session.execute(delete(SavedProperty).where(SavedProperty.user_id == user.id))
    await session.execute(delete(AreaWatch).where(AreaWatch.user_id == user.id))

    # Scrub the identity but keep the row: reviews carry a non-null FK to it,
    # and a hard delete would cascade the public record away.
    user.is_active = False
    user.display_name = None
    user.phone_last4 = None
    # A random hash keeps the unique constraint satisfied while making the
    # original number unrecoverable — and stops a re-signup colliding.
    user.phone_hash = f"deleted:{secrets.token_hex(24)}"

    await session.commit()

    return {
        "deleted": True,
        "reviews_anonymised": anonymised,
        "reviews_withdrawn": removed,
        "photos_deleted": len(photos),
        "message": "Your account is closed. Your phone number is no longer stored.",
    }


class PhoneChangeStart(BaseModel):
    new_phone: str = Field(..., min_length=7, max_length=20)


class PhoneChangeConfirm(BaseModel):
    new_phone: str = Field(..., min_length=7, max_length=20)
    code: str = Field(..., min_length=4, max_length=8)


@router.post("/me/phone/start", status_code=200)
async def start_phone_change(
    payload: PhoneChangeStart,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Send a verification code to a new number.

    The phone hash is the only route back to a user row — plaintext numbers are
    never stored — so losing a number previously meant losing the account, every
    review on it, and the tenancy history that gives those reviews weight.

    Proof of control over the *old* number is the bearer token itself: this
    endpoint requires a live session, which could only have come from an OTP to
    the current number. That is the "both numbers" requirement satisfied without
    a second SMS, and it is why the endpoint is authenticated rather than open.
    """
    try:
        new_hash = security.hash_phone(payload.new_phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if new_hash == user.phone_hash:
        raise HTTPException(
            status_code=409, detail="That is already the number on this account."
        )

    # Refuse early if the number belongs to someone else. Checking here rather
    # than at confirm time avoids sending a code to a number the change can
    # never complete for — and the response is deliberately the same shape as a
    # success would be, so this is not a way to test which numbers are
    # registered.
    taken = (
        await session.execute(select(User).where(User.phone_hash == new_hash))
    ).scalar_one_or_none()

    if taken is None:
        try:
            code = await auth_service.request_otp(
                payload.new_phone, resolve_client_ip(request)
            )
        except OTPThrottled as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)}
            ) from exc
        try:
            await sms.send_otp(security.normalise_phone(payload.new_phone), code)
        except sms.SMSError as exc:
            raise HTTPException(
                status_code=502,
                detail="We couldn't send the code. Check the number and try again.",
            ) from exc

    return {"sent": True, "message": "Enter the code we sent to the new number."}


@router.post("/me/phone/confirm", response_model=MeOut)
async def confirm_phone_change(
    payload: PhoneChangeConfirm,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    """Complete the move once the code for the new number checks out."""
    try:
        new_hash = security.hash_phone(payload.new_phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stored = otp_store.load_otp(new_hash)
    if not stored or not security.verify_otp(new_hash, payload.code, stored):
        if stored and otp_store.register_failed_attempt(new_hash):
            raise HTTPException(
                status_code=400, detail="Too many incorrect attempts — request a new code."
            )
        raise HTTPException(status_code=400, detail="Invalid or expired code.")

    otp_store.clear_otp(new_hash)

    # Re-checked inside the same transaction: the number could have been claimed
    # between start and confirm, and phone_hash is unique. Losing that race
    # would otherwise surface as a 500.
    taken = (
        await session.execute(select(User).where(User.phone_hash == new_hash))
    ).scalar_one_or_none()
    if taken is not None and taken.id != user.id:
        raise HTTPException(
            status_code=409,
            detail="That number is already registered to another account.",
        )

    user.phone_hash = new_hash
    user.phone_last4 = security.phone_last4(payload.new_phone)
    await session.commit()
    await session.refresh(user)
    return MeOut.model_validate(user)

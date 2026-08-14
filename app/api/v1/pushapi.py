"""Push subscription endpoints.

A browser subscribes itself to a push service, then hands us the resulting
endpoint and encryption keys. We store them against the user so area watches can
actually reach a phone.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import PushSubscription, User
from app.db.session import get_session
from app.services import push

router = APIRouter(prefix="/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str = Field(..., max_length=200)
    auth: str = Field(..., max_length=100)


class SubscribeIn(BaseModel):
    endpoint: str = Field(..., max_length=1000)
    keys: PushKeys


class PushConfigOut(BaseModel):
    # Null rather than an empty string when unconfigured: the client checks for
    # a key before offering to subscribe, and "" is easy to treat as present.
    public_key: str | None = None
    enabled: bool = False


@router.get("/config", response_model=PushConfigOut)
async def push_config() -> PushConfigOut:
    """The VAPID public key, or a plain "not enabled".

    Unauthenticated: the public key is public by definition, and the client
    needs to know whether to show the notification prompt at all before it has
    a reason to ask the user to sign in.
    """
    if not push.is_configured():
        return PushConfigOut(enabled=False)
    return PushConfigOut(public_key=push.public_key(), enabled=True)


@router.post("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
async def subscribe(
    payload: SubscribeIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Register this browser for push, or move an existing endpoint to this user.

    Endpoints are unique. Re-subscribing the same browser has to update the row
    rather than insert another, or a user collects a duplicate notification per
    stale row — and a shared or handed-down device would otherwise keep pushing
    a previous owner's watched areas to whoever holds it now.
    """
    if not push.is_configured():
        raise HTTPException(
            status_code=503, detail="Push notifications are not enabled here."
        )

    existing = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == payload.endpoint
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.user_id = user.id
        existing.p256dh = payload.keys.p256dh
        existing.auth = payload.keys.auth
        existing.failure_count = 0
    else:
        session.add(
            PushSubscription(
                user_id=user.id,
                endpoint=payload.endpoint,
                p256dh=payload.keys.p256dh,
                auth=payload.keys.auth,
            )
        )

    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    payload: SubscribeIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stop pushing to this browser.

    Scoped to the caller's own subscriptions: an endpoint string is not a
    secret worth trusting as authorisation to delete somebody else's row.
    """
    row = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == payload.endpoint,
                PushSubscription.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/status")
async def push_status(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """How many browsers this account currently receives notifications on."""
    rows = (
        await session.execute(
            select(PushSubscription).where(PushSubscription.user_id == user.id)
        )
    ).scalars().all()
    return {"enabled": push.is_configured(), "devices": len(rows)}

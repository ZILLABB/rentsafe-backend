"""Auth endpoints (Section XII /auth)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ratelimit import client_ip as resolve_client_ip
from app.config import get_settings
from app.core import security
from app.db.models import User
from app.db.session import get_session
from app.schemas.auth import (
    OTPRequest,
    OTPRequestResponse,
    OTPVerify,
    RefreshRequest,
    TokenPair,
)
from app.services import auth as auth_service
from app.services import sms
from app.services.otp_store import OTPThrottled

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


@router.post("/otp/request", response_model=OTPRequestResponse)
async def request_otp(payload: OTPRequest, request: Request) -> OTPRequestResponse:
    # Resolved through the proxy-aware helper: behind a load balancer the raw
    # socket address is the balancer's, so every user would share one IP quota
    # and throttle each other.
    try:
        code = await auth_service.request_otp(payload.phone, resolve_client_ip(request))
    except OTPThrottled as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Deliver before reporting success. Returning sent=true for a code that
    # never went out is how you lock every user out of a live product while
    # the API reports health.
    try:
        await sms.send_otp(security.normalise_phone(payload.phone), code)
    except sms.SMSError as exc:
        # The code stays in the store; a retry re-sends rather than reissuing,
        # which keeps the resend cooldown meaningful.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We couldn't send your code. Check the number and try again.",
        ) from exc

    # `debug` is force-disabled outside development by the config guard, so the
    # echo cannot leak codes in a deployed environment.
    return OTPRequestResponse(sent=True, dev_code=code if settings.debug else None)


@router.post("/otp/verify", response_model=TokenPair)
async def verify_otp(
    payload: OTPVerify, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    try:
        user, _created = await auth_service.verify_otp(
            session, payload.phone, payload.code
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    access, refresh = auth_service.issue_tokens(user.id)
    return TokenPair(access_token=access, refresh_token=refresh, user_id=user.id)


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    try:
        claims = security.decode_token(payload.refresh_token)
        if claims.get("type") != "refresh":
            raise ValueError("not a refresh token")
        user_id = int(claims["sub"])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token"
        ) from exc

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="invalid refresh token")

    access, new_refresh = auth_service.issue_tokens(user.id)
    return TokenPair(access_token=access, refresh_token=new_refresh, user_id=user.id)

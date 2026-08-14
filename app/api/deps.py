"""Shared FastAPI dependencies — current-user resolution from a JWT."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.db.models import User
from app.db.session import get_session

bearer = HTTPBearer(auto_error=True)
optional_bearer = HTTPBearer(auto_error=False)

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        payload = security.decode_token(creds.credentials)
        if payload.get("type") != "access":
            raise _CREDENTIALS_EXC
        user_id = int(payload["sub"])
    except _CREDENTIALS_EXC.__class__:
        raise
    except Exception as exc:  # jose errors, missing claims, bad int
        raise _CREDENTIALS_EXC from exc

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise _CREDENTIALS_EXC
    return user


async def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(optional_bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """Resolve the caller if they sent a valid token, else ``None``.

    For endpoints that are public but show a little more to a signed-in user —
    e.g. their own not-yet-approved review. A bad token is treated as anonymous
    rather than an error, so a stale token never breaks a public page.
    """
    if creds is None:
        return None
    try:
        return await get_current_user(creds, session)
    except HTTPException:
        return None

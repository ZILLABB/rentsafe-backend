"""Auth service — OTP request/verify and JWT issuance (Section XV)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import security
from app.db.models import User
from app.services import otp_store

settings = get_settings()


async def request_otp(phone: str, client_ip: str | None = None) -> str:
    """Generate + store an OTP for a phone, returning the code.

    The caller (endpoint) sends it via Termii in production. In development the
    code is echoed back in the response so the flow is testable without SMS.

    Raises ``otp_store.OTPThrottled`` when the caller is over quota.
    """
    phone_hash = security.hash_phone(phone)  # validates the number too
    otp_store.check_request_quota(phone_hash, client_ip)
    code = security.generate_otp()
    otp_store.save_otp(phone_hash, security.hash_otp(phone_hash, code))
    return code


async def verify_otp(session: AsyncSession, phone: str, code: str) -> tuple[User, bool]:
    """Verify an OTP, creating the user on first successful login.

    Returns (user, created).
    """
    phone_hash = security.hash_phone(phone)
    stored = otp_store.load_otp(phone_hash)
    if not stored or not security.verify_otp(phone_hash, code, stored):
        if stored and otp_store.register_failed_attempt(phone_hash):
            raise ValueError("too many incorrect attempts — request a new code")
        raise ValueError("invalid or expired code")

    otp_store.clear_otp(phone_hash)

    result = await session.execute(select(User).where(User.phone_hash == phone_hash))
    user = result.scalar_one_or_none()
    created = False
    if user is None:
        user = User(phone_hash=phone_hash, phone_last4=security.phone_last4(phone))
        session.add(user)
        created = True

    user.last_active_at = dt.datetime.now(dt.UTC)
    await session.commit()
    await session.refresh(user)
    return user, created


def issue_tokens(user_id: int) -> tuple[str, str]:
    return (
        security.create_token(user_id, refresh=False),
        security.create_token(user_id, refresh=True),
    )

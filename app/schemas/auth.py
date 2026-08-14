"""Auth request/response schemas (Section XII /auth)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OTPRequest(BaseModel):
    phone: str = Field(..., description="Nigerian phone, e.g. 08031234567")


class OTPRequestResponse(BaseModel):
    sent: bool
    # In development we echo the code so the flow is testable without Termii.
    dev_code: str | None = None


class OTPVerify(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=8)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int


class RefreshRequest(BaseModel):
    refresh_token: str

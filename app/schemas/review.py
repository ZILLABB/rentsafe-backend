"""Review request/response schemas (Section XII /reviews)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class Ratings(BaseModel):
    """The 10 structured dimensions, each 1..5 (Section III)."""

    landlord: int = Field(..., ge=1, le=5)
    agent: int = Field(..., ge=1, le=5)
    property: int = Field(..., ge=1, le=5)
    water: int = Field(..., ge=1, le=5)
    power: int = Field(..., ge=1, le=5)
    security: int = Field(..., ge=1, le=5)
    noise: int = Field(..., ge=1, le=5)
    flooding: int = Field(..., ge=1, le=5)
    neighbourhood: int = Field(..., ge=1, le=5)
    value: int = Field(..., ge=1, le=5)


class ReviewUpdate(BaseModel):
    """An author's amendment. Every field optional — send only what changed.

    Deliberately narrower than ReviewCreate: the property, the tenancy dates and
    the agent are what the review *is*. Changing those would make it a different
    review wearing the same id, and would let someone move an approved
    accusation onto a different building.
    """

    ratings: Ratings | None = None
    text_positives: str | None = None
    text_warnings: str | None = None
    text_negotiation_tips: str | None = None
    rent_amount_kobo: int | None = Field(default=None, ge=0)
    is_anonymous: bool | None = None


class ReviewCreate(BaseModel):
    property_id: str = Field(..., description="Canonical PropertyID, e.g. ETI-LEK-...")
    tenancy_start: dt.date
    tenancy_end: dt.date | None = None
    still_living: bool = False
    rent_amount_kobo: int | None = Field(default=None, ge=0)
    rent_period: str | None = None
    agent_fee_kobo: int | None = Field(default=None, ge=0)
    caution_fee_kobo: int | None = Field(default=None, ge=0)
    agreement_fee_kobo: int | None = Field(default=None, ge=0)
    departure_reason: str | None = None
    ratings: Ratings
    text_positives: str = Field(..., max_length=500)
    text_warnings: str = Field(..., max_length=1000)
    text_negotiation_tips: str | None = Field(default=None, max_length=500)
    is_anonymous: bool = False
    agent_name: str | None = None


class OwnerResponse(BaseModel):
    from_: str = Field(alias="from")
    text: str

    model_config = {"populate_by_name": True}


class ReviewOut(BaseModel):
    id: int
    property_id: str
    tenancy_start: dt.date
    tenancy_end: dt.date | None
    still_living: bool
    rent_amount_kobo: int | None
    agent_fee_kobo: int | None = None
    agent_name: str | None = None
    ratings: Ratings
    aggregate: float
    verification_tier: int
    verified_tenant: bool
    is_anonymous: bool
    display_name: str
    text_positives: str | None
    text_warnings: str | None
    owner_response: OwnerResponse | None = None
    moderation_status: str
    # Only populated on /reviews/mine: the moderator's reason for rejecting a
    # review or asking for edits.
    moderator_note: str | None = None
    # Set when the author amended it after posting. Shown to readers: an account
    # of a named landlord that was rewritten after publication is a different
    # thing from one that wasn't.
    edited_at: dt.datetime | None = None
    # Seconds left in the author's edit window, on /reviews/mine only. Lets the
    # UI offer edit and delete exactly when they will actually work, instead of
    # promising a window and then 409-ing.
    edit_seconds_left: int = 0
    created_at: dt.datetime


class ReviewSubmitResponse(BaseModel):
    review_id: int
    moderation_status: str  # "pending" | "flagged"
    flagged_reasons: list[str]
    message: str

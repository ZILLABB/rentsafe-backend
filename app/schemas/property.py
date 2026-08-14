"""Pydantic request/response models for the property API."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator


class IdentifyRequest(BaseModel):
    """Step 1 (pin drop) + optional Step 3/4 signals from the review wizard."""

    lat: float = Field(..., ge=-90, le=90, description="Pin-drop latitude")
    lng: float = Field(..., ge=-180, le=180, description="Pin-drop longitude")
    lga_code: str = Field(..., min_length=2, max_length=5)
    area_code: str = Field(..., min_length=2, max_length=10)
    address: str | None = Field(default=None, description="Optional typed address")
    photo_hash: str | None = Field(
        default=None, description="Optional building-exterior pHash (hex)"
    )
    location_approximate: bool = Field(
        default=False,
        description=(
            "True when the point is an area centroid rather than the building — "
            "e.g. the street isn't in OpenStreetMap. Distance then carries no "
            "evidence of sameness, so dedup falls back to the typed address."
        ),
    )


class PropertyCandidate(BaseModel):
    property_id: str
    distance_m: float
    address_formal: str | None = None
    address_normalised: str | None = None
    total_reviews: int
    avg_rating: float | None = None
    phash_match: bool = False


class IdentifyResponse(BaseModel):
    """Result of identifying/registering a property from a pin drop.

    ``match`` is one of:
      * ``existing``  — confident single match within the dedup radius; attach review
      * ``ambiguous`` — one or more nearby candidates; ask the user to confirm
      * ``created``   — no match; a new Canonical Property Record was created
    """

    match: str
    property_id: str | None = None
    candidates: list[PropertyCandidate] = Field(default_factory=list)
    needs_review: bool = False
    message: str | None = None


class RatingBreakdown(BaseModel):
    """Weighted mean per dimension, 0 where there's nothing to average yet."""

    landlord: float = 0
    agent: float = 0
    property: float = 0
    water: float = 0
    power: float = 0
    security: float = 0
    noise: float = 0
    flooding: float = 0
    neighbourhood: float = 0
    value: float = 0


class PropertyOut(BaseModel):
    """Full property card — everything the property page hero + tabs need."""

    property_id: str
    lat: float
    lng: float
    lga_code: str | None = None
    lga_name: str | None = None
    neighbourhood_code: str | None = None
    neighbourhood_name: str | None = None
    address_local: str | None = None
    address_formal: str | None = None
    property_type: str | None = None
    bedrooms: int | None = None
    w3w_address: str | None = None
    # "exact" when the coordinate is the building, "area" when it is only the
    # neighbourhood centroid. Screens must not draw a rooftop pin over a guess.
    location_precision: str = "exact"
    geohash_8: str
    flood_zone: str | None = None
    elevation_m: float | None = None
    drainage_dist_m: float | None = None
    avg_rating: float | None = None
    total_reviews: int = 0
    verified_reviews: int = 0
    latest_rent_kobo: int | None = None
    rent_velocity_pct: float | None = None
    area_velocity_pct: float | None = None
    rent_percentile: int | None = None
    power_hours_avg: int | None = None
    security_rating: float | None = None
    high_turnover: bool = False
    traffic_score: str | None = None
    # Most-cited agent across this property's approved reviews, if any.
    agent_slug: str | None = None
    # Read straight off the cached `properties.rating_breakdown` JSON column,
    # which holds the same weighted figures that produce `avg_rating`. Null
    # until a property's first review is approved.
    rating_breakdown: RatingBreakdown = RatingBreakdown()

    @field_validator("rating_breakdown", mode="before")
    @classmethod
    def _empty_breakdown(cls, v: object) -> object:
        return RatingBreakdown() if v is None else v

    model_config = {"from_attributes": True}


class SourceOut(BaseModel):
    """Provenance for one imported field."""

    field: str
    source: str
    licence: str | None = None
    url: str | None = None
    fetched_at: dt.datetime


class RentPointOut(BaseModel):
    year: int
    rent_kobo: int
    area_avg_kobo: int | None = None


class FloodEventOut(BaseModel):
    when: str
    severity: str
    quote: str
    evidence: str | None = None


class EnvironmentOut(BaseModel):
    flood_zone: str | None
    flood_report_count: int
    elevation_m: float | None
    drainage_dist_m: float | None
    power_hours_avg: int | None
    flood_events: list[FloodEventOut]

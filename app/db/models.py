"""SQLAlchemy ORM models.

Portable across SQLite (local dev — zero setup) and PostgreSQL (production).
On Postgres the canonical DDL in ``sql/001_schema.sql`` adds the PostGIS
GEOGRAPHY column + GIST index; the ORM keeps lat/lng as the source of truth and
``location_wkt`` as a plain-text mirror so the same models run on both engines.
Spatial dedup queries fall back to geohash-prefix + haversine (see
``services/identity.py``) which is exact at the 15–100m radii we use.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# All timestamps are timezone-aware. Storing naive datetimes and comparing them
# against `datetime.now(timezone.utc)` silently shifts every window query by the
# server's UTC offset — which previously broke both the review-velocity check and
# the moderation SLA clock.
TZDateTime = DateTime(timezone=True)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Return ``value`` as an aware UTC datetime.

    Postgres round-trips ``timestamptz`` as aware, but SQLite has no timezone
    type and hands back naive datetimes even for a ``timezone=True`` column. Any
    arithmetic against ``datetime.now(UTC)`` therefore has to normalise first,
    or it raises "can't subtract offset-naive and offset-aware datetimes" on
    SQLite while working fine on Postgres.
    """
    if value is None:
        return None
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


class Base(DeclarativeBase):
    pass


class LGA(Base):
    __tablename__ = "lgas"

    code: Mapped[str] = mapped_column(String(5), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    flood_risk: Mapped[str | None] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(50), default="Lagos")
    # Boundary centroid from OSM — used to attach a new address to an LGA.
    centroid_lat: Mapped[float | None] = mapped_column(Numeric(10, 8))
    centroid_lng: Mapped[float | None] = mapped_column(Numeric(11, 8))


class Neighbourhood(Base):
    __tablename__ = "neighbourhoods"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    lga_code: Mapped[str | None] = mapped_column(ForeignKey("lgas.code"))
    centroid_lat: Mapped[float | None] = mapped_column(Numeric(10, 8))
    centroid_lng: Mapped[float | None] = mapped_column(Numeric(11, 8))
    avg_rent_1bed: Mapped[int | None] = mapped_column(Integer)
    avg_rent_2bed: Mapped[int | None] = mapped_column(Integer)
    avg_rent_3bed: Mapped[int | None] = mapped_column(Integer)
    avg_rating: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_power_hours: Mapped[int | None] = mapped_column(SmallInteger)
    avg_security: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_agent_fee_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    commute_vi_min: Mapped[int | None] = mapped_column(SmallInteger)
    flood_risk: Mapped[str | None] = mapped_column(String(20))
    total_properties: Mapped[int] = mapped_column(Integer, default=0)
    total_reviews: Mapped[int] = mapped_column(Integer, default=0)
    # Corridor risk: areas served by a single exit route gridlock as a unit.
    bottleneck_title: Mapped[str | None] = mapped_column(String(120))
    bottleneck_detail: Mapped[str | None] = mapped_column(Text)


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[str] = mapped_column(String(25), unique=True, nullable=False)
    geohash_7: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    geohash_8: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    lat: Mapped[float] = mapped_column(Numeric(10, 8), nullable=False)
    lng: Mapped[float] = mapped_column(Numeric(11, 8), nullable=False)
    location_wkt: Mapped[str | None] = mapped_column(Text)
    # "exact" when the coordinate is the building, "area" when it is only the
    # neighbourhood centroid because the street isn't mapped. Stored so screens
    # can avoid drawing a rooftop pin over a guess.
    location_precision: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="exact"
    )
    address_formal: Mapped[str | None] = mapped_column(Text)
    address_local: Mapped[str | None] = mapped_column(Text)
    address_normalised: Mapped[str | None] = mapped_column(Text)
    w3w_address: Mapped[str | None] = mapped_column(String(60))
    lga_code: Mapped[str | None] = mapped_column(ForeignKey("lgas.code"), index=True)
    neighbourhood_code: Mapped[str | None] = mapped_column(
        ForeignKey("neighbourhoods.code"), index=True
    )
    lga: Mapped[LGA | None] = relationship(lazy="raise")
    neighbourhood: Mapped[Neighbourhood | None] = relationship(lazy="raise")
    property_type: Mapped[str | None] = mapped_column(String(30))
    bedrooms: Mapped[int | None] = mapped_column(SmallInteger)
    flood_zone: Mapped[str | None] = mapped_column(String(20))
    elevation_m: Mapped[float | None] = mapped_column(Numeric(6, 2))
    drainage_dist_m: Mapped[float | None] = mapped_column(Numeric(8, 2))
    photo_hash: Mapped[str | None] = mapped_column(String(64))
    street_view_url: Mapped[str | None] = mapped_column(Text)
    avg_rating: Mapped[float | None] = mapped_column(Numeric(3, 2))
    # Weighted per-dimension means, cached by `recompute_property_scores` so the
    # breakdown shown to users comes from the same aggregation as the headline
    # score (and so listing a property costs no extra queries).
    rating_breakdown: Mapped[dict[str, float] | None] = mapped_column(JSON)
    total_reviews: Mapped[int] = mapped_column(Integer, default=0)
    verified_reviews: Mapped[int] = mapped_column(Integer, default=0)
    latest_rent_kobo: Mapped[int | None] = mapped_column(Integer)
    rent_velocity_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    area_velocity_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    rent_percentile: Mapped[int | None] = mapped_column(SmallInteger)
    liquidity_score: Mapped[float | None] = mapped_column(Numeric(3, 1))
    traffic_score: Mapped[str | None] = mapped_column(String(10))
    high_turnover: Mapped[bool] = mapped_column(Boolean, default=False)
    power_hours_avg: Mapped[int | None] = mapped_column(SmallInteger)
    security_rating: Mapped[float | None] = mapped_column(Numeric(3, 2))
    status: Mapped[str] = mapped_column(String(20), default="active")
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("properties.id"))
    created_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    phone_last4: Mapped[str | None] = mapped_column(String(4))
    nin_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str | None] = mapped_column(String(60))
    is_anonymous_default: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String(20), default="tenant")
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    trust_score: Mapped[float] = mapped_column(Numeric(3, 2), default=0.50)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())
    last_active_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    # When this user last opened their alerts, so "unread" means something.
    # The nav badge was previously hardcoded on, which made it signal nothing.
    alerts_read_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_normalised: Mapped[str | None] = mapped_column(String(100), index=True)
    slug: Mapped[str | None] = mapped_column(String(100), unique=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    company_name: Mapped[str | None] = mapped_column(String(200))
    operating_areas: Mapped[list[str] | None] = mapped_column(JSON)
    lasrera_number: Mapped[str | None] = mapped_column(String(30))
    lasrera_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    avg_rating_transparency: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_rating_honesty: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_rating_fee_fairness: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_rating_responsiveness: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_rating_professionalism: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_rating_overall: Mapped[float | None] = mapped_column(Numeric(3, 2))
    avg_fee_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    total_reviews: Mapped[int] = mapped_column(Integer, default=0)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(Text)
    profile_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class Review(Base):
    __tablename__ = "reviews"

    __table_args__ = (
        # Every property page and the moderation queue filter on this pair.
        Index("ix_reviews_property_status", "property_id", "moderation_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"), index=True)

    tenancy_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    tenancy_end: Mapped[dt.date | None] = mapped_column(Date)
    still_living: Mapped[bool] = mapped_column(Boolean, default=False)
    rent_amount_kobo: Mapped[int | None] = mapped_column(Integer)
    rent_period: Mapped[str | None] = mapped_column(String(20))
    agent_fee_kobo: Mapped[int | None] = mapped_column(Integer)
    caution_fee_kobo: Mapped[int | None] = mapped_column(Integer)
    agreement_fee_kobo: Mapped[int | None] = mapped_column(Integer)
    departure_reason: Mapped[str | None] = mapped_column(String(50))

    rating_landlord: Mapped[int | None] = mapped_column(SmallInteger)
    rating_agent: Mapped[int | None] = mapped_column(SmallInteger)
    rating_property: Mapped[int | None] = mapped_column(SmallInteger)
    rating_water: Mapped[int | None] = mapped_column(SmallInteger)
    rating_power: Mapped[int | None] = mapped_column(SmallInteger)
    rating_security: Mapped[int | None] = mapped_column(SmallInteger)
    rating_noise: Mapped[int | None] = mapped_column(SmallInteger)
    rating_flooding: Mapped[int | None] = mapped_column(SmallInteger)
    rating_neighbourhood: Mapped[int | None] = mapped_column(SmallInteger)
    rating_value: Mapped[int | None] = mapped_column(SmallInteger)

    text_positives: Mapped[str | None] = mapped_column(Text)
    text_warnings: Mapped[str | None] = mapped_column(Text)
    text_negotiation_tips: Mapped[str | None] = mapped_column(Text)

    verification_tier: Mapped[int] = mapped_column(SmallInteger, default=1)
    verified_tenant: Mapped[bool] = mapped_column(Boolean, default=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str | None] = mapped_column(String(60))
    owner_response: Mapped[str | None] = mapped_column(Text)
    owner_response_from: Mapped[str | None] = mapped_column(String(20))

    moderation_status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )
    # Why the automated pre-check held this review, so the moderation queue can
    # show the reason rather than re-deriving it from a duplicated word list.
    flag_reasons: Mapped[list[str] | None] = mapped_column(JSON)
    flag_count: Mapped[int] = mapped_column(Integer, default=0)
    # Set when the author amends their own review inside the edit window. Shown
    # beside the review: a reader deciding how much weight to give an account of
    # a landlord is entitled to know it was rewritten after publication.
    edited_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now()
    )


class PropertyPhoto(Base):
    """A tenant-uploaded photo of a property or of evidence.

    Photos are user-generated content aimed at a real, identifiable building,
    so they go through the same moderation gate as review text: nothing is
    public until a human approves it.
    """

    __tablename__ = "property_photos"

    __table_args__ = (
        Index("ix_photos_property_status", "property_id", "moderation_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    review_id: Mapped[int | None] = mapped_column(ForeignKey("reviews.id"), index=True)

    storage_key: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    # Perceptual hash — same building from a different angle lands nearby.
    phash: Mapped[str | None] = mapped_column(String(32), index=True)
    caption: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="property")  # property/evidence

    moderation_status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class AreaWatch(Base):
    """A tenant watching a neighbourhood for new activity.

    This is what turns /alerts from a global firehose into something personal:
    someone hunting in Yaba doesn't need every flood report in Lekki.
    """

    __tablename__ = "area_watches"

    __table_args__ = (
        UniqueConstraint("user_id", "area_code", name="uq_watch_user_area"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    area_code: Mapped[str] = mapped_column(
        ForeignKey("neighbourhoods.code"), nullable=False, index=True
    )
    # Optional thresholds so a watch can be "tell me about flooding" rather
    # than "tell me about everything".
    notify_reviews: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_floods: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_agent_flags: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class SavedProperty(Base):
    """A tenant's bookmark. One row per user per property."""

    __tablename__ = "saved_properties"

    __table_args__ = (
        UniqueConstraint("user_id", "property_id", name="uq_saved_user_property"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class DataSource(Base):
    """Provenance for anything imported from an external dataset.

    RentSafe's pitch is that its numbers can be trusted, which obliges it to be
    able to say where each one came from and under what licence. Attribution
    isn't optional for OSM-derived data either — ODbL requires it.
    """

    __tablename__ = "data_sources"

    __table_args__ = (Index("ix_data_sources_subject", "subject_type", "subject_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # e.g. "property" / "lga" / "neighbourhood", and the row's natural key.
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(40), nullable=False)
    field: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. elevation_m
    source: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. OpenStreetMap
    licence: Mapped[str | None] = mapped_column(String(60))
    url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class CommuteReport(Base):
    """A tenant's own door-to-door commute time (Section VII).

    The product's claim is that lived experience beats a routing API's estimate,
    so this is the primary source. `google_estimate_min` is stored alongside for
    the comparison, and is null until a Routes integration fills it in — we do
    not invent it.
    """

    __tablename__ = "commute_reports"

    __table_args__ = (
        Index("ix_commute_property_dest", "property_id", "destination_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    review_id: Mapped[int | None] = mapped_column(ForeignKey("reviews.id"))

    destination_code: Mapped[str] = mapped_column(String(10), nullable=False)
    # "am_rush" | "pm_rush" | "midday" | "weekend" — when the trip was made.
    departure_window: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)  # car/bus/brt/keke/ferry/bike
    minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    google_estimate_min: Mapped[int | None] = mapped_column(SmallInteger)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class TransitOption(Base):
    """Public transport within walking distance of a property (Section VII)."""

    __tablename__ = "transit_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # brt/bus/ferry/keke/rail
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    distance_m: Mapped[int | None] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class CommuteDestination(Base):
    """Work destinations tenants commute to, shared across properties."""

    __tablename__ = "commute_destinations"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    lat: Mapped[float | None] = mapped_column(Numeric(10, 8))
    lng: Mapped[float | None] = mapped_column(Numeric(11, 8))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class AgentClaim(Base):
    """An agent asking to take control of their own profile.

    ``profile_claimed`` existed on ``Agent`` from the start and nothing could
    ever set it, so the right-of-reply feature — which authorises on a matching
    ``phone_hash`` — was unreachable by the people it exists for.

    Deliberately admin-approved rather than automatic. Granting someone the
    power to reply on behalf of a named agent, and to be treated as that agent
    thereafter, cannot rest on them simply asserting it: the obvious abuse is
    claiming a rival's profile, or a landlord claiming the agent who let their
    property in order to answer criticism of themselves.
    """

    __tablename__ = "agent_claims"

    __table_args__ = (
        # One live claim per person per agent; re-applying updates the row.
        UniqueConstraint("agent_id", "user_id", name="uq_claim_agent_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    # What the claimant offers as proof. Checked by a human out of band — a
    # LASRERA number is verifiable against the register, a CAC number against
    # the companies registry.
    lasrera_number: Mapped[str | None] = mapped_column(String(30))
    evidence_note: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )


class PushSubscription(Base):
    """A browser push endpoint belonging to one user.

    Area watches personalised the in-app feed but delivered nothing to a phone,
    so a flood report for the street you are about to sign a lease on only
    existed if you happened to open the app that week.

    One row per browser, not per user: people use a phone and a laptop, and a
    subscription is revoked per device by the browser, not by us.
    """

    __tablename__ = "push_subscriptions"

    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_endpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    # The push service URL the browser gave us. Unique: re-subscribing the same
    # browser must update the row rather than accumulate duplicates, which is
    # how people end up getting four copies of every notification.
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    # Encryption material from the browser. Without both, a push cannot be
    # encrypted, and an unencrypted Web Push is rejected by every push service.
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
    # Consecutive delivery failures. A browser that has been uninstalled keeps
    # its endpoint alive for a while and then 410s forever; counting lets us
    # drop dead rows instead of retrying them on every alert.
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ModerationAction(Base):
    """Audit trail for human moderation decisions.

    Reviews here can be defamation-adjacent, so "who approved this, and when"
    needs to be answerable in writing long after the fact. Append-only.
    """

    __tablename__ = "moderation_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Exactly one of these is set: the trail covers reviews and photos alike.
    review_id: Mapped[int | None] = mapped_column(ForeignKey("reviews.id"), index=True)
    photo_id: Mapped[int | None] = mapped_column(
        ForeignKey("property_photos.id"), index=True
    )
    moderator_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class RentHistory(Base):
    __tablename__ = "rent_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    review_id: Mapped[int | None] = mapped_column(ForeignKey("reviews.id"), index=True)
    period_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount_kobo: Mapped[int] = mapped_column(Integer, nullable=False)
    area_avg_kobo: Mapped[int | None] = mapped_column(Integer)
    payment_type: Mapped[str | None] = mapped_column(String(20))
    agent_fee_kobo: Mapped[int | None] = mapped_column(Integer)
    caution_fee_kobo: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class FloodEvent(Base):
    __tablename__ = "flood_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), nullable=False, index=True
    )
    when_label: Mapped[str] = mapped_column(String(20), nullable=False)  # "OCT 2024"
    severity: Mapped[str] = mapped_column(String(10), nullable=False)  # major/moderate/minor
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(String(10))  # video/photo/null

"""Route tests for the moderation boundary.

These cover the rule that matters most legally: un-approved review content is
never served to the public, and never moves a property's public score.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import geohash
from app.db.models import LGA, Neighbourhood, Property
from tests.conftest import ADMIN_PHONE, TENANT_PHONE, auth, login

PROPERTY_ID = "ETI-LEK-7F3A2B-0041"

CLEAN_RATINGS = {
    "landlord": 4, "agent": 4, "property": 4, "water": 4, "power": 4,
    "security": 4, "noise": 4, "flooding": 4, "neighbourhood": 4, "value": 4,
}


@pytest.fixture
async def property_row(session_factory):
    async with session_factory() as s:
        s.add(LGA(code="ETI", name="Eti-Osa", flood_risk="VeryHigh"))
        s.add(Neighbourhood(code="LEK", name="Lekki Phase 1", lga_code="ETI"))
        gh8, gh7 = geohash.encode_pair(6.4474, 3.4736)
        prop = Property(
            property_id=PROPERTY_ID,
            geohash_7=gh7, geohash_8=gh8,
            lat=6.4474, lng=3.4736,
            address_local="12A Admiralty Way, Lekki Phase 1",
            lga_code="ETI", neighbourhood_code="LEK",
            flood_zone="High",
        )
        s.add(prop)
        await s.commit()
        await s.refresh(prop)
        return prop


def _payload(**over):
    body = {
        "property_id": PROPERTY_ID,
        "tenancy_start": "2023-01-01",
        "tenancy_end": "2024-12-31",
        "still_living": False,
        "rent_amount_kobo": 150_000_000,
        "ratings": CLEAN_RATINGS,
        "text_positives": "Water is constant and the estate security is serious.",
        "text_warnings": "Rent went up at renewal with short notice.",
        "is_anonymous": False,
    }
    body.update(over)
    return body


async def test_flagged_review_is_not_public(client, users, property_row):
    """A review held for legal risk must not be readable by anonymous callers."""
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        "/reviews",
        json=_payload(text_warnings="The agent is a fraudster and a thief"),
        headers=auth(token),
    )
    assert r.status_code == 201
    assert r.json()["moderation_status"] == "flagged"
    assert "legally_risky_language" in r.json()["flagged_reasons"]

    public = await client.get(f"/properties/{PROPERTY_ID}/reviews")
    assert public.status_code == 200
    assert public.json() == []


async def test_pending_review_is_not_public(client, users, property_row):
    """Even a clean review stays private until a moderator approves it."""
    token = await login(client, TENANT_PHONE)
    r = await client.post("/reviews", json=_payload(), headers=auth(token))
    assert r.json()["moderation_status"] == "pending"

    public = await client.get(f"/properties/{PROPERTY_ID}/reviews")
    assert public.json() == []


async def test_author_sees_their_own_unapproved_review(client, users, property_row):
    """The submitter shouldn't think their review vanished."""
    token = await login(client, TENANT_PHONE)
    await client.post("/reviews", json=_payload(), headers=auth(token))

    mine = await client.get(f"/properties/{PROPERTY_ID}/reviews", headers=auth(token))
    assert len(mine.json()) == 1
    assert mine.json()[0]["moderation_status"] == "pending"


async def test_unapproved_review_does_not_move_the_public_score(
    client, users, property_row
):
    token = await login(client, TENANT_PHONE)
    before = (await client.get(f"/properties/{PROPERTY_ID}")).json()
    assert before["total_reviews"] == 0

    await client.post(
        "/reviews",
        json=_payload(ratings=dict.fromkeys(CLEAN_RATINGS, 1)),
        headers=auth(token),
    )

    after = (await client.get(f"/properties/{PROPERTY_ID}")).json()
    assert after["avg_rating"] == before["avg_rating"]
    assert after["total_reviews"] == 0


async def test_approval_publishes_and_updates_the_score(
    client, users, property_row, session_factory
):
    tenant = await login(client, TENANT_PHONE)
    submitted = await client.post("/reviews", json=_payload(), headers=auth(tenant))
    review_id = submitted.json()["review_id"]

    admin = await login(client, ADMIN_PHONE)
    r = await client.patch(
        f"/admin/moderation/reviews/{review_id}",
        json={"action": "approve", "note": "checked"},
        headers=auth(admin),
    )
    assert r.status_code == 200

    public = await client.get(f"/properties/{PROPERTY_ID}/reviews")
    assert len(public.json()) == 1

    prop = (await client.get(f"/properties/{PROPERTY_ID}")).json()
    assert prop["total_reviews"] == 1
    assert prop["avg_rating"] == pytest.approx(4.0)
    # The breakdown must come from the same aggregation as the headline score.
    assert prop["rating_breakdown"]["water"] == pytest.approx(4.0)


async def test_rejection_hides_the_review(client, users, property_row):
    tenant = await login(client, TENANT_PHONE)
    review_id = (
        await client.post("/reviews", json=_payload(), headers=auth(tenant))
    ).json()["review_id"]

    admin = await login(client, ADMIN_PHONE)
    r = await client.patch(
        f"/admin/moderation/reviews/{review_id}",
        json={"action": "reject", "note": "Unsubstantiated claim about a named agent."},
        headers=auth(admin),
    )
    assert r.status_code == 200, r.text

    assert (await client.get(f"/properties/{PROPERTY_ID}/reviews")).json() == []
    # Not even to its author.
    mine = await client.get(f"/properties/{PROPERTY_ID}/reviews", headers=auth(tenant))
    assert mine.json() == []


async def test_rejecting_without_a_reason_is_refused(client, users, property_row):
    """The author is entitled to know why, so the note isn't optional."""
    tenant = await login(client, TENANT_PHONE)
    review_id = (
        await client.post("/reviews", json=_payload(), headers=auth(tenant))
    ).json()["review_id"]

    admin = await login(client, ADMIN_PHONE)
    r = await client.patch(
        f"/admin/moderation/reviews/{review_id}",
        json={"action": "reject"},
        headers=auth(admin),
    )
    assert r.status_code == 422
    # And the review is untouched.
    mine = await client.get(f"/properties/{PROPERTY_ID}/reviews", headers=auth(tenant))
    assert mine.json()[0]["moderation_status"] == "pending"


async def test_request_edits_bounces_the_review_back_with_the_reason(
    client, users, property_row
):
    """"Ask edits" was a button with no handler; it now has a real third state."""
    tenant = await login(client, TENANT_PHONE)
    review_id = (
        await client.post("/reviews", json=_payload(), headers=auth(tenant))
    ).json()["review_id"]

    admin = await login(client, ADMIN_PHONE)
    r = await client.patch(
        f"/admin/moderation/reviews/{review_id}",
        json={"action": "request_edits", "note": "Please remove the agent's name."},
        headers=auth(admin),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "needs_edits"

    # Still not public...
    assert (await client.get(f"/properties/{PROPERTY_ID}/reviews")).json() == []

    # ...but the author sees it, with the moderator's reason attached.
    mine = (await client.get("/reviews/mine", headers=auth(tenant))).json()
    assert mine[0]["moderation_status"] == "needs_edits"
    assert mine[0]["moderator_note"] == "Please remove the agent's name."


async def test_queue_reports_why_an_item_was_flagged(client, users, property_row):
    """Reasons come from the stored verdict, not a duplicated frontend word list."""
    tenant = await login(client, TENANT_PHONE)
    await client.post(
        "/reviews",
        json=_payload(text_warnings="The agent is a fraudster and a thief"),
        headers=auth(tenant),
    )

    admin = await login(client, ADMIN_PHONE)
    queue = (await client.get("/admin/moderation/reviews", headers=auth(admin))).json()
    assert queue[0]["flag_reasons"] == ["legally_risky_language"]
    # Moderators are publishing a score, so the score has to be on the card.
    assert queue[0]["aggregate"] == pytest.approx(4.0)
    assert queue[0]["property_address"] == "12A Admiralty Way, Lekki Phase 1"


async def test_moderation_is_audited(client, users, property_row, session_factory):
    from app.db.models import ModerationAction

    tenant = await login(client, TENANT_PHONE)
    review_id = (
        await client.post("/reviews", json=_payload(), headers=auth(tenant))
    ).json()["review_id"]

    admin = await login(client, ADMIN_PHONE)
    await client.patch(
        f"/admin/moderation/reviews/{review_id}",
        json={"action": "reject", "note": "unsubstantiated allegation"},
        headers=auth(admin),
    )

    async with session_factory() as s:
        actions = (await s.execute(select(ModerationAction))).scalars().all()
    assert len(actions) == 1
    assert actions[0].action == "reject"
    assert actions[0].previous_status == "pending"
    assert actions[0].note == "unsubstantiated allegation"
    assert actions[0].moderator_id == users["admin"].id


async def test_duplicate_text_is_flagged(client, users, property_row):
    """The copy-paste review-bombing pattern routes to a human."""
    first = await login(client, TENANT_PHONE)
    await client.post("/reviews", json=_payload(), headers=auth(first))

    from tests.conftest import OTHER_PHONE

    second = await login(client, OTHER_PHONE)
    r = await client.post("/reviews", json=_payload(), headers=auth(second))
    assert "duplicate_text" in r.json()["flagged_reasons"]


@pytest.mark.parametrize(
    "text",
    [
        "The agent is a fraudster",          # English
        "Na 419 man, e chop my money",       # Nigerian Pidgin
        "Ole ni oniwun ile yii",             # Yorùbá — "the landlord is a thief"
    ],
)
async def test_defamation_patterns_across_languages(client, users, property_row, text):
    """The app invites reviews in three languages; the filter has to read all three."""
    token = await login(client, TENANT_PHONE)
    r = await client.post(
        "/reviews", json=_payload(text_warnings=text), headers=auth(token)
    )
    assert r.json()["moderation_status"] == "flagged", f"not caught: {text!r}"


async def test_queue_lists_pending_reviews_with_an_sla_age(
    client, users, property_row
):
    """Regression: the queue crashed on SQLite comparing an aware `now` against
    a naive `created_at`, so it 500'd as soon as anything was actually queued."""
    tenant = await login(client, TENANT_PHONE)
    await client.post("/reviews", json=_payload(), headers=auth(tenant))

    admin = await login(client, ADMIN_PHONE)
    r = await client.get("/admin/moderation/reviews", headers=auth(admin))
    assert r.status_code == 200, r.text

    queue = r.json()
    assert len(queue) == 1
    assert queue[0]["status"] == "pending"
    assert queue[0]["property_id"] == PROPERTY_ID
    # Just submitted, so the SLA clock should read ~0 and never be negative.
    assert 0 <= queue[0]["submitted_hours_ago"] < 1


async def test_velocity_check_counts_recent_reviews(client, users, property_row):
    """The 24h window compares a stored timestamp against an aware bound."""
    token = await login(client, TENANT_PHONE)
    for i in range(4):
        r = await client.post(
            "/reviews",
            json=_payload(text_positives=f"Distinct positive note number {i} here."),
            headers=auth(token),
        )
        assert r.status_code == 201, r.text
    # The fourth submission is over VELOCITY_LIMIT_24H (3).
    assert "velocity" in r.json()["flagged_reasons"]

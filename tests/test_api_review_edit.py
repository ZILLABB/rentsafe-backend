"""Amending and withdrawing your own review.

The Profile page promised a 48-hour edit/delete window from the start. No
endpoint implemented it, so a tenant who got a fact wrong about a named landlord
— the exact thing the terms make them personally liable for — had no way to
correct it, and no way to withdraw it.

Three things have to hold at once:

* an author can fix their own mistake inside the window,
* nobody can touch anyone else's review, ever, and
* an edit cannot be used to launder text past moderation.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.db.models import Review
from app.services import reviews as review_service
from tests.conftest import OTHER_PHONE, TENANT_PHONE, auth, login
from tests.test_api_photos import (  # noqa: F401 — fixture used by name
    PROPERTY_ID,
    property_row,
)

RATINGS = {
    "landlord": 3, "agent": 3, "property": 3, "water": 3, "power": 3,
    "security": 3, "noise": 3, "flooding": 3, "neighbourhood": 3, "value": 3,
}


async def _submit(client, token, property_id, **over) -> int:
    body = {
        "property_id": property_id,
        "tenancy_start": "2024-01-01",
        "still_living": True,
        "rent_amount_kobo": 100_000_000,
        "ratings": RATINGS,
        "text_positives": "The compound is quiet and the water runs daily.",
        "text_warnings": "Parking is tight in the evenings.",
        "is_anonymous": False,
    }
    body.update(over)
    r = await client.post("/reviews", json=body, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()["review_id"]


async def test_author_can_edit_inside_the_window(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    r = await client.patch(
        f"/reviews/{review_id}",
        json={"text_warnings": "Parking is tight, and the gate closes at 10pm."},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text

    mine = await client.get("/reviews/mine", headers=auth(token))
    row = next(x for x in mine.json() if x["id"] == review_id)
    assert "gate closes" in row["text_warnings"]
    assert row["edited_at"] is not None


async def test_author_can_withdraw_inside_the_window(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    r = await client.delete(f"/reviews/{review_id}", headers=auth(token))
    assert r.status_code == 204

    mine = await client.get("/reviews/mine", headers=auth(token))
    assert all(x["id"] != review_id for x in mine.json())


async def test_a_stranger_cannot_touch_your_review(client, users, property_row):
    """404, not 403: whether a review id exists is not a stranger's business."""
    owner = await login(client, TENANT_PHONE)
    review_id = await _submit(client, owner, PROPERTY_ID)

    other = await login(client, OTHER_PHONE)
    edit = await client.patch(
        f"/reviews/{review_id}",
        json={"text_warnings": "Something the author never wrote."},
        headers=auth(other),
    )
    assert edit.status_code == 404

    delete = await client.delete(f"/reviews/{review_id}", headers=auth(other))
    assert delete.status_code == 404


async def test_editing_requires_a_session(client, users, property_row):
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    r = await client.patch(f"/reviews/{review_id}", json={"text_warnings": "x"})
    assert r.status_code in (401, 403)


async def test_the_window_closes_after_48_hours(
    client, users, property_row, session_factory
):
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    async with session_factory() as s:
        review = (
            await s.execute(select(Review).where(Review.id == review_id))
        ).scalar_one()
        review.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=49)
        await s.commit()

    r = await client.patch(
        f"/reviews/{review_id}",
        json={"text_warnings": "Too late."},
        headers=auth(token),
    )
    assert r.status_code == 409
    assert "48-hour" in r.json()["detail"]


async def test_an_edit_goes_back_through_moderation(client, users, property_row):
    """Otherwise the window is a way to launder an accusation past the check.

    Publish something bland, wait for approval, then rewrite it.
    """
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    r = await client.patch(
        f"/reviews/{review_id}",
        json={"text_warnings": "The landlord is a fraud and a criminal."},
        headers=auth(token),
    )
    assert r.status_code == 200
    assert r.json()["moderation_status"] in ("pending", "flagged")
    assert r.json()["flagged_reasons"], "defamatory edit passed unflagged"


async def test_an_edited_review_leaves_the_public_score(
    client, users, property_row, session_factory
):
    """An edit un-approves the review, so the property's aggregate must drop it."""
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    async with session_factory() as s:
        review = (
            await s.execute(select(Review).where(Review.id == review_id))
        ).scalar_one()
        review.moderation_status = "approved"
        await s.commit()
        await review_service.recompute_property_scores(s, review.property_id)

    await client.patch(
        f"/reviews/{review_id}",
        json={"text_positives": "Revised account of living here."},
        headers=auth(token),
    )

    async with session_factory() as s:
        review = (
            await s.execute(select(Review).where(Review.id == review_id))
        ).scalar_one()
    assert review.moderation_status != "approved"


async def test_a_review_with_a_reply_can_no_longer_be_edited(
    client, users, property_row, session_factory
):
    """Rewriting a statement a landlord already answered strands their reply."""
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    async with session_factory() as s:
        review = (
            await s.execute(select(Review).where(Review.id == review_id))
        ).scalar_one()
        review.owner_response = "We have since fixed the gate."
        review.owner_response_from = "landlord"
        await s.commit()

    r = await client.patch(
        f"/reviews/{review_id}",
        json={"text_warnings": "Rewritten after the reply."},
        headers=auth(token),
    )
    assert r.status_code == 409


async def test_editing_cannot_move_a_review_to_another_property(
    client, users, property_row
):
    """The property is what the review *is*. Changing it would move an
    approved accusation onto a different building."""
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    r = await client.patch(
        f"/reviews/{review_id}",
        json={"property_id": "ETI-LEK-0000AA-9999", "text_warnings": "moved"},
        headers=auth(token),
    )
    # The field is simply not part of the update schema, so it is ignored.
    assert r.status_code == 200

    mine = await client.get("/reviews/mine", headers=auth(token))
    row = next(x for x in mine.json() if x["id"] == review_id)
    assert row["property_id"] == PROPERTY_ID


async def test_mine_reports_how_long_is_left_to_edit(client, users, property_row):
    """The UI offers edit/delete only when they will actually work."""
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    mine = await client.get("/reviews/mine", headers=auth(token))
    row = next(x for x in mine.json() if x["id"] == review_id)
    assert 0 < row["edit_seconds_left"] <= 48 * 3600


@pytest.mark.parametrize("bad", [0, 6, -1])
async def test_edited_ratings_are_still_validated(client, users, property_row, bad):
    token = await login(client, TENANT_PHONE)
    review_id = await _submit(client, token, PROPERTY_ID)

    r = await client.patch(
        f"/reviews/{review_id}",
        json={"ratings": {**RATINGS, "landlord": bad}},
        headers=auth(token),
    )
    assert r.status_code == 422
